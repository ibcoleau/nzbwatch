import asyncio
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from app.core import Store, Engine, matching, parse_rss, fingerprint

RSS = b'''<?xml version="1.0"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
<item><title>My.Series.S01E02.1080p.WEB</title><guid>A</guid>
<link>http://hydra:5076/getnzb/a</link><pubDate>Sun, 01 Nov 2026 02:00:00 GMT</pubDate>
<newznab:attr name="size" value="1500000000" /></item>
<item><title>Other.Series.S01E01.CAM</title><link>http://hydra:5076/getnzb/b</link><enclosure length="10000000" url="http://hydra:5076/getnzb/b" /></item>
</channel></rss>'''


def payload(**kwargs):
    data = dict(name='Series',query='My Series',enabled=True,mode='auto',match_mode='all',
                required_words='1080p',excluded_words='CAM',categories='5000',sab_category='tv',
                min_mb=100,max_mb=3000,max_age_days=None,max_grabs=1,poll_minutes=30)
    data.update(kwargs)
    return data


def test_parse_filter_and_stable_dedupe():
    result = parse_rss(RSS)
    assert len(result) == 2
    assert result[0]['size_bytes'] == 1_500_000_000
    assert matching(payload(), result[0])
    assert not matching(payload(), result[1])
    assert fingerprint('My.Series.S01E02.1080p.WEB') == fingerprint('my series s01e02 1080p web')
    assert not matching(payload(match_mode='phrase',query='My Series Wrong'), result[0])


def test_store_crud_and_history(tmp_path):
    s = Store(str(tmp_path/'watch.sqlite3'))
    wid = s.save_watch(payload())
    assert s.watch(wid)['max_grabs'] == 1
    data = payload(name='Changed',mode='notify')
    s.save_watch(data,wid)
    assert s.watch(wid)['name'] == 'Changed'
    assert s.watch(wid)['mode'] == 'notify'
    assert s.recent()['counts']['watches'] == 1
    s.delete_watch(wid)
    assert not s.watches()


def test_full_integration_mock(tmp_path):
    requests = []
    nzb = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"> </nzb>'
    def mock(request):
        requests.append((request.method,request.url.path,parse_qs(request.url.query.decode())))
        if request.url.host == 'hydra' and request.url.path == '/api':
            return httpx.Response(200,content=RSS)
        if request.url.host == 'hydra' and request.url.path == '/getnzb/a':
            return httpx.Response(200,content=nzb)
        if request.url.host == 'sab' and request.url.path == '/api':
            args = parse_qs(request.url.query.decode())
            if args.get('mode') == ['addfile']:
                assert b'filename="watch.nzb"' in request.content
                return httpx.Response(200,json={'status':True,'nzo_ids':['SABnzbd_nzo_123']})
            if args.get('mode') == ['history']:
                return httpx.Response(200,json={'history':{'slots':[{'nzo_id':'SABnzbd_nzo_123','status':'Completed'}]}})
        return httpx.Response(404)
    s = Store(str(tmp_path/'test.sqlite3'))
    config = {'hydra_url':'http://hydra:5076','hydra_key':'secret','sab_url':'http://sab:8080','sab_key':'sabsecret','pushover_key':'','pushover_user':'','notification_types':set()}
    engine = Engine(s,config)
    async def run():
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(mock))
        try:
            wid=s.save_watch(payload())
            await engine.run_checks([wid])
            result=s.recent()['matches'][0]
            assert result['status']=='queued'
            assert s.watch(wid)['grab_count']==1
            assert s.watch(wid)['enabled']==0  # Automatically paused after one successful queue
            await engine.run_checks([wid])
            assert len([x for x in requests if x[1]=='/getnzb/a']) == 1
            await engine.refresh_downloads()
            assert s.recent()['matches'][0]['status']=='completed'
        finally:
            await engine.client.aclose()
    asyncio.run(run())


def test_reject_external_host(tmp_path):
    s=Store(str(tmp_path/'test.sqlite3'))
    wid=s.save_watch(payload())
    with s.conn() as db:
        db.execute("INSERT INTO matches(watch_id,title,fingerprint,link,found_at) VALUES(?,?,?,?,?)", (wid,'Bad test',fingerprint('Bad test'),'http://elsewhere/nzb','2026-11-01T00:00:00+00:00'))
    config={'hydra_url':'http://hydra:5076','hydra_key':'secret','sab_url':'http://sab:8080','sab_key':'sabsecret','pushover_key':'','pushover_user':'','notification_types':set()}
    engine=Engine(s,config)
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200)))
        try:
            mid=s.recent()['matches'][0]['id']
            assert await engine.queue_match(mid)=='failed'
            assert s.match(mid)['status']=='failed'
            assert 'outside' in s.match(mid)['note']
        finally:await engine.client.aclose()
    asyncio.run(run())


def test_hydra_redirect_uses_sab_addurl(tmp_path):
    seen=[]
    def handler(req):
        seen.append(req)
        if req.url.host=='hydra' and req.url.path=='/api':
            return httpx.Response(200,content=RSS)
        if req.url.host=='hydra' and req.url.path=='/getnzb/a':
            return httpx.Response(302,headers={'Location':'https://indexer.example/download/private'})
        if req.url.host=='sab':
            assert parse_qs(req.url.query.decode()).get('mode')==['addurl']
            assert 'apikey=secret' in parse_qs(req.url.query.decode())['name'][0]
            assert 'indexer.example' not in str(req.url)
            return httpx.Response(200,json={'status':True,'nzo_ids':['SAB_456']})
        return httpx.Response(404)
    s=Store(str(tmp_path/'test.sqlite3'))
    config={'hydra_url':'http://hydra:5076','hydra_key':'secret','sab_url':'http://sab:8080','sab_key':'sabsecret','pushover_key':'','pushover_user':'','notification_types':set()}
    engine=Engine(s,config)
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try:
            wid=s.save_watch(payload())
            await engine.run_checks([wid])
            assert s.recent()['matches'][0]['status']=='queued'
        finally: await engine.client.aclose()
    asyncio.run(run())
