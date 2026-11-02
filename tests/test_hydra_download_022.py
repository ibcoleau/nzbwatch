"""Regression tests for Hydra error payloads and stale RSS API keys."""
import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core import Engine, Store, authenticated_hydra_url, nzb_upload_info

VALID_NZB = b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'
CONFIG = {'hydra_url': 'http://hydra:5076/nzbhydra2', 'hydra_key':'new-key',
          'sab_url':'http://sab:8080','sab_key':'sab-key',
          'pushover_key':'','pushover_user':'','notification_types':set()}


def watch(store):
    return store.save_watch({'name':'Example Show', 'query':'Example Show S01E01', 'enabled':1,
        'mode':'auto','match_mode':'all','required_words':'','excluded_words':'',
        'categories':'','sab_category':'','min_mb':None,'max_mb':None,'max_age_days':None,
        'max_grabs':1, 'poll_minutes':0,'broad_search':1,'resolution_mode':'off'})


@pytest.mark.parametrize('query', ['', '?apikey=', '?apikey=old-key', '?APIKEY=old-key&foo=bar'])
def test_replaces_any_embedded_api_key(query):
    url=authenticated_hydra_url('http://hydra:5076/nzbhydra2', '/nzbhydra2/getnzb/api/123'+query, 'new-key')
    values=parse_qs(urlsplit(url).query)
    assert values['apikey']==['new-key']
    assert 'APIKEY' not in values
    if 'foo=bar' in query: assert values['foo']==['bar']
    assert 'old-key' not in url


def test_rejects_external_origin():
    with pytest.raises(ValueError, match='outside'):
        authenticated_hydra_url('http://hydra:5076/nzbhydra2','http://attacker:5076/grab','key')


def test_newznab_error_description_and_code_redacted():
    error=b'<error code="100" description="Invalid API key: abc-secret" />'
    with pytest.raises(ValueError, match='code 100') as e:
        nzb_upload_info(error, 'application/xml', secrets=('abc-secret',))
    assert 'Invalid API key' in str(e.value)
    assert 'abc-secret' not in str(e.value)
    assert '[redacted]' in str(e.value)


def test_newznab_error_cannot_echo_credentials_in_url():
    error=b'<error code="300" description="Cannot fetch http://hydra/get?apikey=abc-secret" />'
    with pytest.raises(ValueError) as e:
        nzb_upload_info(error, 'text/xml', secrets=('abc-secret',))
    assert 'code 300' in str(e.value)
    assert 'abc-secret' not in str(e.value)
    assert 'http://hydra' not in str(e.value)


def test_existing_blank_key_link_queues_with_config_key(tmp_path):
    store = Store(str(tmp_path/'db.sqlite'))
    wid=watch(store)
    rss=b'''<?xml version="1.0"?><rss><channel><item>
        <title>Example.Show.S01E01.1080p</title>
        <enclosure url="http://hydra:5076/nzbhydra2/getnzb/api/id123?apikey=" type="application/x-nzb"/>
        </item></channel></rss>'''
    seen=[]
    def handler(req):
        if req.url.host=='hydra' and req.url.path=='/nzbhydra2/api':
            return httpx.Response(200, content=rss)
        if req.url.host=='hydra' and req.url.path=='/nzbhydra2/getnzb/api/id123':
            seen.append(str(req.url))
            if req.url.params.get('apikey')!='new-key':
                return httpx.Response(200, content=b'<error code="100" description="Invalid API key"/>')
            return httpx.Response(200, content=VALID_NZB)
        if req.url.host=='sab':
            assert req.url.params['mode']=='addfile'
            return httpx.Response(200,json={'status':True,'nzo_ids':['nzo123']})
        raise AssertionError(f'Unexpected request to {req.url.host}{req.url.path}')
    engine=Engine(store,CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try: await engine.run_checks([wid])
        finally: await engine.client.aclose()
    asyncio.run(run())
    assert len(seen)==1 and 'apikey=new-key' in seen[0]
    assert store.recent()['matches'][0]['status']=='queued'


def test_redirect_to_indexer_keeps_authenticated_hydra_url(tmp_path):
    store=Store(str(tmp_path/'db.sqlite'))
    wid=watch(store)
    with store.conn() as db:
        db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,status,found_at) VALUES(?,?,?,?,?,?)',
                   (wid,'Example.Show.S01E01.1080p','fp123',
                   'http://hydra:5076/nzbhydra2/getnzb/api/id123?apikey=old-key', 'found', '2026-11-02T00:00:00+00:00'))
    seen=[]
    def handler(req):
        if req.url.host=='hydra':
            assert req.url.params.get('apikey')=='new-key'
            return httpx.Response(302, headers={'Location':'https://indexer.invalid/download?id=123'})
        if req.url.host=='sab':
            assert req.url.params['mode']=='addurl'
            name=req.url.params['name']
            assert name.startswith('http://hydra:5076/nzbhydra2/getnzb/api/id123')
            assert parse_qs(urlsplit(name).query)['apikey']==['new-key']
            assert 'indexer.invalid' not in name
            seen.append(name)
            return httpx.Response(200,json={'status':True,'nzo_ids':['nzo123']})
        raise AssertionError()
    engine=Engine(store,CONFIG.copy())
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try: assert await engine.queue_match(store.recent()['matches'][0]['id']) == 'queued'
        finally: await engine.client.aclose()
    asyncio.run(run())
    assert seen


def test_removed_intro_tagline():
    html = (Path(__file__).resolve().parents[1]/'app/static/index.html').read_text()
    assert 'Keep an eye on everything.' not in html
    assert 'NZBHydra2 scans, SABnzbd downloads and Pushover alerts in one place.' not in html
