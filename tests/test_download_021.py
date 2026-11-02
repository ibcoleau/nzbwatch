"""Regression tests for NZBHydra2 200 HTML, enclosure links and archive responses."""
import asyncio
import gzip
import io
import zipfile
from urllib.parse import parse_qs

import httpx
import pytest

from app.core import Engine, Store, fingerprint, now, nzb_upload_info, parse_rss

VALID_NZB = b'<?xml version="1.0"?>\n<!-- xml comment -->\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'
CONFIG = {'hydra_url':'http://hydra:5076/nzbhydra2', 'hydra_key':'hydra-key', 'sab_url':'http://sab:8080',
          'sab_key':'sab-key','pushover_key':'','pushover_user':'','notification_types':set()}


def new_watch(store, *, mode='auto'):
    return store.save_watch({'name':'Example Show', 'query':'Example Show S01E01', 'enabled':1,
        'mode':mode, 'match_mode':'all', 'required_words':'', 'excluded_words':'',
        'categories':'', 'sab_category':'', 'min_mb':None,'max_mb':None, 'max_age_days':None,
        'max_grabs':1,'poll_minutes':0,'broad_search':1,'resolution_mode':'off'})


def xml_rss():
    return b'''<?xml version="1.0"?><rss><channel><item>
      <title>Example.Show.S01E01.1080p.WEB</title>
      <link>http://hydra:5076/nzbhydra2/details/some-id</link>
      <enclosure url="http://hydra:5076/nzbhydra2/getnzb/api/some-id" type="application/x-nzb" />
      </item></channel></rss>'''


def test_enclosure_url_preferred_over_details_link():
    assert parse_rss(xml_rss())[0]['link'] == 'http://hydra:5076/nzbhydra2/getnzb/api/some-id'
    assert parse_rss(b'<rss><channel><item><title>Test</title><link>http://hydra/nzb</link></item></channel></rss>')[0]['link'] == 'http://hydra/nzb'


def test_nzb_formats_xml_gzip_zip_rar_and_rejections():
    padded = b'<?xml version="1.0"?>\n<!-- ' + b'x'*4100 + b' -->\n<nzb></nzb>'
    assert nzb_upload_info(padded, 'application/xml') == ('watch.nzb', 'application/x-nzb')
    assert nzb_upload_info(VALID_NZB)[0] == 'watch.nzb'
    assert nzb_upload_info(gzip.compress(VALID_NZB))[0] == 'watch.nzb.gz'
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w') as z:
        z.writestr('something.nzb', VALID_NZB)
    assert nzb_upload_info(zip_buf.getvalue())[0] == 'watch.nzb.zip'
    assert nzb_upload_info(b'Rar!\x1a\x07\x00more-bytes')[0] == 'watch.nzb.rar'
    for data,typ,label in ((b'<html><body>Login</body></html>', 'text/html','HTML'),
                           (b'{"error":"login"}', 'application/json','JSON'),
                           (b'<error description="invalid key"/>','text/xml','error'),
                           (b'<rss></rss>', 'application/xml','RSS'),
                           (b'', '', 'empty')):
        with pytest.raises(ValueError, match=label):
            nzb_upload_info(data,typ)


def test_queue_uses_enclosure_and_not_details_page(tmp_path):
    requests=[]
    def handler(req):
        requests.append(req)
        if req.url.host=='hydra' and req.url.path=='/nzbhydra2/api':
            return httpx.Response(200, content=xml_rss())
        if req.url.host=='hydra' and req.url.path=='/nzbhydra2/getnzb/api/some-id':
            return httpx.Response(200, content=VALID_NZB,headers={'Content-Type':'application/x-nzb'})
        if req.url.host=='sab':
            assert parse_qs(req.url.query.decode())['mode']==['addfile']
            assert b'filename="watch.nzb"' in req.content
            return httpx.Response(200,json={'status':True,'nzo_ids':['nzo-id']})
        return httpx.Response(200, text='<html>details page</html>',headers={'Content-Type':'text/html'})
    store=Store(str(tmp_path/'db.sqlite'))
    watch=new_watch(store)
    engine=Engine(store, CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try:
            await engine.run_checks([watch])
        finally: await engine.client.aclose()
    asyncio.run(run())
    assert store.recent()['matches'][0]['status']=='queued'
    assert not any(req.url.path.startswith('/nzbhydra2/details') for req in requests)


def test_failed_match_refreshes_old_link_and_retries(tmp_path):
    store=Store(str(tmp_path/'db.sqlite'))
    wid=new_watch(store)
    title='Example.Show.S01E01.1080p.WEB'
    with store.conn() as db:
        db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,status,found_at) VALUES(?,?,?,?,?,?)',
                   (wid,title,fingerprint(title),'http://hydra:5076/nzbhydra2/details/some-id','failed',now()))
    downloads=[]
    def handler(req):
        if req.url.path=='/nzbhydra2/api':
            return httpx.Response(200,content=xml_rss())
        if req.url.path=='/nzbhydra2/getnzb/api/some-id':
            downloads.append(str(req.url))
            return httpx.Response(200,content=VALID_NZB)
        if req.url.host=='sab':
            return httpx.Response(200,json={'status':True,'nzo_ids':['new-nzo-id']})
        raise AssertionError('Should not request details link: '+str(req.url))
    engine=Engine(store,CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try: await engine.run_checks([wid])
        finally: await engine.client.aclose()
    asyncio.run(run())
    m=store.recent()['matches'][0]
    assert m['status']=='queued'
    assert downloads and m['nzo_id']=='new-nzo-id'


def test_html_is_rejected_with_actionable_message(tmp_path):
    store=Store(str(tmp_path/'db.sqlite'))
    wid=new_watch(store)
    title='Example.Show.S01E01.1080p.WEB'
    with store.conn() as db:
        db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,status,found_at) VALUES(?,?,?,?,?,?)',
                   (wid,title,fingerprint(title),'http://hydra:5076/nzbhydra2/getnzb/api/bad','found',now()))
    def handler(req):
        if req.url.host=='hydra':
            return httpx.Response(200,content=b'<html>Login required</html>',headers={'Content-Type':'text/html'})
        raise AssertionError('SAB should not receive invalid HTML')
    engine=Engine(store,CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try:
            assert await engine.queue_match(store.recent()['matches'][0]['id'])=='failed'
        finally: await engine.client.aclose()
    asyncio.run(run())
    assert 'HTML' in store.recent()['matches'][0]['note']
    assert 'hydra-key' not in str(store.recent())


def test_gzip_download_is_uploaded_with_correct_extension(tmp_path):
    store=Store(str(tmp_path/'db.sqlite'))
    wid=new_watch(store)
    title='Example.Show.S01E01.1080p.WEB'
    with store.conn() as db:
        db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,status,found_at) VALUES(?,?,?,?,?,?)',
                   (wid,title,fingerprint(title),'http://hydra:5076/nzbhydra2/getnzb/api/comp','found',now()))
    def handler(req):
        if req.url.host=='hydra':
            return httpx.Response(200,content=gzip.compress(VALID_NZB),headers={'Content-Type':'application/gzip'})
        if req.url.host=='sab':
            assert b'filename="watch.nzb.gz"' in req.content
            return httpx.Response(200,json={'status':True,'nzo_ids':['nzo-id']})
        raise AssertionError()
    engine=Engine(store,CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try: assert await engine.queue_match(store.recent()['matches'][0]['id'])=='queued'
        finally: await engine.client.aclose()
    asyncio.run(run())
