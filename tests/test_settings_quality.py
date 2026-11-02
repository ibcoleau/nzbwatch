"""Regression tests for v0.2 online configuration and quality-aware searches."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.core import Engine, Store, simplify_query, parse_resolution, search_query, quality_allowed, quality_sort_key
from app.settings import SettingsFile


def test_first_boot_migrates_environment_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv('HYDRA_URL', 'http://oldhydra:5076/nzbhydra2')
    monkeypatch.setenv('HYDRA_API_KEY', 'original-hydra-key')
    monkeypatch.setenv('SAB_URL', 'http://sab:8080')
    monkeypatch.setenv('SAB_API_KEY', 'original-sab-key')
    setting=SettingsFile(str(tmp_path/'config.json'))
    assert setting.data['hydra_key']=='original-hydra-key'
    assert setting.data['hydra_url']=='http://oldhydra:5076/nzbhydra2'
    assert setting.data['sab_key']=='original-sab-key'
    assert setting.path.stat().st_mode & 0o777 == 0o600
    assert 'original-hydra-key' not in json.dumps(setting.public())
    # UI update changes dict in-place (same dict engine holds) and atomically saves.
    config=setting.data
    setting.update({'hydra_url':'http://newhydra:5076/nzbhydra2', 'hydra_key':None, 'scan_interval_minutes':7})
    assert config is setting.data
    assert config['hydra_url']=='http://newhydra:5076/nzbhydra2'
    assert config['hydra_key']=='original-hydra-key'
    monkeypatch.setenv('HYDRA_API_KEY','env-should-not-override-file')
    again=SettingsFile(str(tmp_path/'config.json'))
    assert again.data['hydra_key']=='original-hydra-key'
    assert again.data['scan_interval_minutes']==7
    setting.update({'hydra_key':''})
    assert setting.data['hydra_key']==''
    assert not setting.public()['configured_secrets']['hydra_key']


def test_invalid_settings_do_not_change_saved_file(tmp_path):
    settings=SettingsFile(str(tmp_path/'config.json'))
    original=settings.path.read_bytes()
    for patch in (
        {'scan_interval_minutes':0}, {'resolution_order':['1080p','1080p']},
        {'hydra_url':'file:///etc/passwd'}, {'sab_url':'http://host:8080/api?apikey=secret'},
        {'max_queues_per_scan':300},
    ):
        with pytest.raises(ValueError):
            settings.update(patch)
        assert settings.path.read_bytes()==original


def test_release_query_cleaning_and_resolution_detection():
    cases={
        'Movie.Name.2024.1080p.WEB-DL-GROUP':'Movie Name 2024',
        'My.Series.S01E03.2160p.WEBRip.x265-KTR':'My Series S01E03',
        'Some.Title.XXX.720p.MP4-P2P':'Some Title',
        'Movie.Name.2024.1080p-GROUP':'Movie Name 2024',
        'Ocean Life 2024':'Ocean Life 2024',
    }
    for text,expected in cases.items():
        assert simplify_query(text)==expected, (text, simplify_query(text))
    assert parse_resolution('Sample.4K.WEB-DL')=='2160p'
    assert parse_resolution('show 1920x1080 WEB')=='1080p'
    assert parse_resolution('show 1280x720 WEB')=='720p'
    assert parse_resolution('year 2160 documentary') is None
    assert parse_resolution('Movie 480p')=='480p'


def test_resolution_preference_and_min_fallback():
    settings={'resolution_enabled':True,'resolution_order':['1080p','2160p','720p'],
              'fallback_min_resolution':'720p','allow_unknown_resolution':False}
    releases=[{'title':f'My Show S01E01 {r}', 'published':None, 'resolution':r}
              for r in ('720p','2160p','1080p','480p',None)]
    w={'resolution_mode':'inherit'}
    available=[r for r in releases if quality_allowed(w,r,settings)]
    assert [r['resolution'] for r in sorted(available,key=lambda x:quality_sort_key(x,settings))]==['1080p','2160p','720p']
    assert quality_allowed({'resolution_mode':'off'}, releases[3], settings)
    assert search_query({'query':'Movie.Name.2024.1080p.WEB-DL-GROUP','broad_search':True}, {'clean_search_terms':True,'strip_release_groups':True}) == 'Movie Name 2024'


def test_existing_database_migration_preserves_watches(tmp_path):
    import sqlite3
    path=str(tmp_path/'old.sqlite3')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE watches (id INTEGER PRIMARY KEY,name TEXT NOT NULL,query TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,mode TEXT NOT NULL DEFAULT "auto",match_mode TEXT NOT NULL DEFAULT "all",required_words TEXT NOT NULL DEFAULT "",excluded_words TEXT NOT NULL DEFAULT "",categories TEXT NOT NULL DEFAULT "",sab_category TEXT NOT NULL DEFAULT "",min_mb REAL,max_mb REAL,max_age_days INTEGER,max_grabs INTEGER NOT NULL DEFAULT 1,grab_count INTEGER NOT NULL DEFAULT 0,poll_minutes INTEGER NOT NULL DEFAULT 30,last_checked TEXT,next_run TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
        db.execute("INSERT INTO watches(name,query,poll_minutes,created_at,updated_at) VALUES('Old','Movie.Name.1080p',15,'now','now')")
    store=Store(path)
    w=store.watches()[0]
    assert w['poll_minutes']==15
    assert w['broad_search']==0
    assert w['resolution_mode']=='inherit'
    assert 'resolution' in {x[1] for x in sqlite3.connect(path).execute('PRAGMA table_info(matches)')}


def test_engine_queues_best_result_not_first(tmp_path):
    payload=b'''<?xml version="1.0"?><rss><channel>
    <item><title>My.Series.S01E01.720p.WEB</title><link>http://hydra/get/720</link></item>
    <item><title>My.Series.S01E01.2160p.WEB</title><link>http://hydra/get/2160</link></item>
    <item><title>My.Series.S01E01.1080p.WEB</title><link>http://hydra/get/1080</link></item>
    <item><title>My.Series.S01E01.480p.WEB</title><link>http://hydra/get/480</link></item>
    </channel></rss>'''
    queries=[];downloads=[]
    def server(req):
        if req.url.host=='hydra' and req.url.path=='/api':
            queries.append(parse_qs(req.url.query.decode())['q'][0])
            return httpx.Response(200,content=payload)
        if req.url.host=='hydra':
            downloads.append(req.url.path)
            return httpx.Response(200,content=b'<nzb></nzb>')
        if req.url.host=='sab':
            return httpx.Response(200,json={'status':True,'nzo_ids':['SAB_123']})
        return httpx.Response(404)
    settings=SettingsFile(str(tmp_path/'config.json'))
    settings.update({'hydra_url':'http://hydra','hydra_key':'secret','sab_url':'http://sab','sab_key':'secret',
                     'resolution_enabled':True,'resolution_order':['1080p','2160p','720p'],
                     'fallback_min_resolution':'720p'})
    store=Store(str(tmp_path/'db.sqlite3'))
    wid=store.save_watch({'name':'My Series','query':'My.Series.S01E01.1080p.WEB-GROUP',
        'enabled':True,'mode':'auto','match_mode':'all','required_words':'','excluded_words':'',
        'categories':'','sab_category':'','min_mb':None,'max_mb':None,'max_age_days':None,
        'max_grabs':1,'poll_minutes':0,'broad_search':True,'resolution_mode':'inherit'})
    async def run():
        engine=Engine(store, settings.data)
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(server))
        try: await engine.run_checks([wid])
        finally: await engine.client.aclose()
    asyncio.run(run())
    assert queries==['My Series S01E01']
    assert downloads==['/get/1080']
    assert store.watch(wid)['grab_count']==1
    assert store.recent()['matches'][0]['resolution']=='1080p'


def test_dedupe_across_resolutions_and_watches(tmp_path):
    from app.core import content_fingerprint, fingerprint, now
    assert content_fingerprint('Show.Name.S02E03.1080p.WEB-GROUP') == content_fingerprint('Show.Name.S02E03.2160p.WEB-GROUP')
    assert content_fingerprint('Show.Name.S02E04.1080p.WEB') != content_fingerprint('Show.Name.S02E03.1080p.WEB')
    s=Store(str(tmp_path/'database.sqlite3'))
    fields={'name':'A', 'query':'Show Name S02E03','enabled':1,'mode':'auto','match_mode':'all','required_words':'','excluded_words':'',
      'categories':'','sab_category':'','min_mb':None,'max_mb':None,'max_age_days':None,'max_grabs':0,'poll_minutes':0,'broad_search':True,'resolution_mode':'inherit'}
    first=s.save_watch(fields)
    second=s.save_watch({**fields,'name':'B'})
    title1='Show.Name.S02E03.1080p.WEB-GROUP'; title2='Show.Name.S02E03.2160p.WEB-GROUP'
    with s.conn() as db:
        for wid,title in ((first,title1),(second,title2)):
            db.execute('INSERT INTO matches(watch_id,title,fingerprint,link,found_at) VALUES(?,?,?,?,?)',(wid,title,fingerprint(title),'http://hydra/get',now()))
        mid=db.execute('SELECT id FROM matches WHERE watch_id=?',(first,)).fetchone()[0]
        db.execute('INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at,content_key) VALUES(?,?,?,?,?)',(fingerprint(title1),mid,'SAB111',now(),content_fingerprint(title1)))
        other=db.execute('SELECT id FROM matches WHERE watch_id=?',(second,)).fetchone()[0]
    config={'hydra_url':'http://hydra','hydra_key':'key','sab_url':'http://sab','sab_key':'sabkey',
       'pushover_key':'','pushover_user':'','notification_types':[],'dedupe_across_resolutions':True}
    engine=Engine(s,config)
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('Should not call network'))))
        try:return await engine.queue_match(other)
        finally:await engine.client.aclose()
    assert asyncio.run(run()) == 'duplicate'
    assert s.match(other)['status']=='skipped'
