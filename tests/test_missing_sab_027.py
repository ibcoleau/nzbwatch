"""Missing SAB jobs can be assumed complete, but must never be called verified."""
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
import httpx
import pytest
from app.core import Engine, Store, fingerprint, now
from app.settings import SettingsFile


def engine_fixture(tmp_path, responder, **settings):
    store = Store(str(tmp_path / 'db.sqlite3'))
    wid = store.save_watch({'name':'Example', 'query':'Example Show', 'enabled':False,
        'mode':'auto', 'match_mode':'all', 'required_words':'', 'excluded_words':'',
        'categories':'', 'sab_category':'', 'min_mb':None, 'max_mb':None,
        'max_age_days':None, 'max_grabs':1, 'poll_minutes':30})
    with store.conn() as db:
        db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,status,nzo_id,found_at,queued_at) '
                   'VALUES(?,?,?,?,?,?,?,?)', (wid,'Example.Show.S01E01.1080p', fingerprint('Example.Show.S01E01.1080p'),
                    'http://hydra/getnzb/1', 'queued', 'SABnzbd_nzo_123', now(), now()))
    cfg = {'sab_url':'http://sab:8080','sab_key':'test-sab-key','hydra_key':'test-hydra-key',
           'pushover_user':'', 'pushover_key':'', 'notification_types':[],
           'sab_missing_policy':'keep_queued', 'sab_missing_grace_minutes':60}
    cfg.update(settings)
    engine = Engine(store,cfg)
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return engine,store


def refresh(engine):
    async def go():
        await engine.refresh_downloads()
    asyncio.run(go())


def missing_response(request):
    mode = parse_qs(request.url.query.decode())['mode'][0]
    return httpx.Response(200,json={mode:{'slots':[]}})


def backdate_missing(store):
    old = (datetime.now(timezone.utc)-timedelta(minutes=75)).isoformat(timespec='seconds')
    with store.conn() as db:
        db.execute('UPDATE matches SET sab_missing_since=?',(old,))


def test_assumed_completion_requires_persistent_missing_and_grace(tmp_path):
    e,s = engine_fixture(tmp_path,missing_response,sab_missing_policy='assume_completed')
    try:
        refresh(e)
        m=s.recent()['matches'][0]
        assert m['status']=='queued'
        with s.conn() as db:
            assert db.execute('SELECT sab_missing_since FROM matches').fetchone()[0]
        backdate_missing(s)
        refresh(e)
        m=s.recent()['matches'][0]
        assert m['status']=='assumed_completed'
        assert 'not verified' in m['note'].lower()
        assert m['finished_at']
        assert s.recent()['counts']['completed']==1
        assert sum(x['type']=='assumed_completed' for x in s.recent()['events'])==1
        refresh(e)
        assert sum(x['type']=='assumed_completed' for x in s.recent()['events'])==1
    finally: asyncio.run(e.client.aclose())


def test_disabled_policy_never_assumes(tmp_path):
    e,s=engine_fixture(tmp_path,missing_response)
    try:
        backdate_missing(s)
        refresh(e)
        assert s.recent()['matches'][0]['status']=='queued'
    finally: asyncio.run(e.client.aclose())


def test_queue_reappears_resets_absence_timer(tmp_path):
    def queue_response(req):
        mode=parse_qs(req.url.query.decode())['mode'][0]
        if mode=='queue':
            return httpx.Response(200,json={'queue':{'slots':[{'nzo_id':'SABnzbd_nzo_123','status':'Downloading'}]}})
        return httpx.Response(200,json={'history':{'slots':[]}})
    e,s=engine_fixture(tmp_path,queue_response,sab_missing_policy='assume_completed')
    try:
        backdate_missing(s)
        refresh(e)
        with s.conn() as db:
            assert db.execute('SELECT sab_missing_since FROM matches').fetchone()[0] is None
        assert s.recent()['matches'][0]['status']=='queued'
    finally: asyncio.run(e.client.aclose())


def test_api_error_must_not_trigger_assumption(tmp_path):
    e,s=engine_fixture(tmp_path,lambda _:httpx.Response(503),sab_missing_policy='assume_completed')
    try:
        backdate_missing(s)
        refresh(e)
        assert s.recent()['matches'][0]['status']=='queued'
    finally: asyncio.run(e.client.aclose())


def test_malformed_api_not_treated_as_missing(tmp_path):
    e,s=engine_fixture(tmp_path,lambda _:httpx.Response(200,json={'error_message':'not authorized'}),sab_missing_policy='assume_completed')
    try:
        backdate_missing(s)
        refresh(e)
        assert s.recent()['matches'][0]['status']=='queued'
    finally: asyncio.run(e.client.aclose())


def test_sab_failure_overrides_missing_policy(tmp_path):
    def failed(req):
        return httpx.Response(200,json={'history':{'slots':[{'nzo_id':'SABnzbd_nzo_123','status':'Failed','fail_message':'Disk full'}]}})
    e,s=engine_fixture(tmp_path,failed,sab_missing_policy='assume_completed')
    try:
        backdate_missing(s)
        refresh(e)
        assert s.recent()['matches'][0]['status']=='failed_download'
    finally: asyncio.run(e.client.aclose())


def test_settings_persist_policy_and_grace(tmp_path):
    settings=SettingsFile(str(tmp_path/'config.json'))
    assert settings.data['sab_missing_policy']=='keep_queued'
    settings.update({'sab_missing_policy':'assume_completed','sab_missing_grace_minutes':15})
    again=SettingsFile(str(tmp_path/'config.json'))
    assert again.data['sab_missing_policy']=='assume_completed'
    assert again.data['sab_missing_grace_minutes']==15
    with pytest.raises(ValueError):
        again.update({'sab_missing_grace_minutes':0})
    with pytest.raises(ValueError):
        again.update({'sab_missing_policy':'unsafe'})
