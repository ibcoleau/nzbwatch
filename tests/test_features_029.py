"""v0.2.9: privacy, retention, event pagination and persistent theme regression."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

import pytest
from app.settings import SettingsFile
from app.core import Store, notification_content


def watch_payload(name="A"):
    return dict(name=name,query=f"{name} Movie 2026",enabled=True,mode="auto",
        match_mode="all",required_words="",excluded_words="",categories="",
        sab_category="",min_mb=None,max_mb=None,max_age_days=None,
        max_grabs=1,poll_minutes=0,broad_search=True,resolution_mode="inherit")


def add_match(s,w,status,age_days=20):
    stamp=(datetime.now(timezone.utc)-timedelta(days=age_days)).isoformat(timespec='seconds')
    with s.conn() as db:
        cur=db.execute('''INSERT INTO matches(watch_id,title,fingerprint,size_bytes,link,status,found_at,queued_at,finished_at,nzo_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',(w,f'Example {status}',status+str(w),100,'http://hydra/get',status,stamp,stamp,stamp if status in ('completed','assumed_completed','failed_download') else None,'ID'+status))
        return cur.lastrowid


def test_defaults_validation_theme_and_retention(tmp_path):
    cfg=SettingsFile(str(tmp_path/'config.json'))
    assert cfg.data['history_retention_days']==14
    assert cfg.data['completed_watch_retention_days']==7
    assert cfg.data['theme_mode']=='dark'
    assert cfg.data['pushover_privacy']=='full'
    cfg.update(dict(history_retention_days=10,completed_watch_retention_days=4,
                    pushover_privacy='generic',theme_mode='light',theme_color='#ABCDEF'))
    assert cfg.data['theme_color']=='#abcdef'
    assert SettingsFile(str(tmp_path/'config.json')).public()['theme_color']=='#abcdef'
    original=cfg.path.read_bytes()
    for bad in ({'theme_color':'red; background: red'}, {'theme_color':'#abc'},
                {'theme_mode':'automatic'}, {'pushover_privacy':'none'},
                {'history_retention_days':-1},{'completed_watch_retention_days':5000},
                {'completed_watch_retention_days':True}):
        with pytest.raises(ValueError):cfg.update(bad)
        assert cfg.path.read_bytes()==original


def test_notification_privacy_all_kinds():
    for kind in ('found','queued','completed','failed'):
        title='filename secrets 1080p'; body='My Private Watch: file.name.with.tags.1080p'
        assert notification_content(title,body,kind,'My Private Watch','full')==(title,body)
        t,m=notification_content(title,body,kind,'My Private Watch','watch_only')
        assert 'My Private Watch' in m
        assert '1080p' not in m+t and 'file.name' not in m+t
        t,m=notification_content(title,body,kind,'My Private Watch','generic')
        assert 'My Private Watch' not in m+t
        assert '1080p' not in m+t
        assert 'file.name' not in m+t
    assert notification_content('original','anything','found','Watch','generic')[1]=='NZBWatch found a download for something'


def test_old_records_pruned_but_active_and_ledger_preserved(tmp_path):
    s=Store(str(tmp_path/'d.db'))
    w=s.save_watch(watch_payload())
    done=add_match(s,w,'completed')
    fail=add_match(s,w,'failed')
    queued=add_match(s,w,'queued')
    pending=add_match(s,w,'found')
    with s.conn() as db:
        db.execute("INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at) VALUES (?,?,?,?)",('unique-ledger',done,'nzo','2000-01-01T00:00:00+00:00'))
        db.execute("INSERT INTO events(type,message,created_at) VALUES (?,?,?)",('test','old','2000-01-01T00:00:00+00:00'))
        db.execute("INSERT INTO events(type,message,created_at) VALUES (?,?,?)",('test','recent',datetime.now(timezone.utc).isoformat(timespec='seconds')))
    result=s.prune_history(14,7)
    assert result['matches']==2
    assert result['events']==1
    with s.conn() as db:
        assert {r[0] for r in db.execute('SELECT id FROM matches')}=={queued,pending}
        assert db.execute('SELECT count(*) FROM grabs').fetchone()[0]==1
        assert db.execute('SELECT message FROM events').fetchone()[0]=='recent'


def test_completed_watch_removed_only_when_finished(tmp_path):
    s=Store(str(tmp_path/'d.db'))
    complete=s.save_watch(watch_payload('Done'))
    waiting=s.save_watch(watch_payload('Waiting'))
    failed=s.save_watch(watch_payload('Failed'))
    mid=add_match(s,complete,'completed',10)
    add_match(s,waiting,'queued',10)
    add_match(s,failed,'failed_download',10)
    with s.conn() as db:
        db.execute('UPDATE watches SET enabled=0,grab_count=1 WHERE id IN (?,?,?)',(complete,waiting,failed))
        db.execute("INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at) VALUES (?,?,?,?)",('dedupe',mid,'nzo','2000-01-01T00:00:00+00:00'))
    r=s.prune_history(14,7)
    assert r['watches']==1
    assert s.watch(complete) is None
    assert s.watch(waiting) is not None
    assert s.watch(failed) is not None
    with s.conn() as db:
        assert db.execute('SELECT COUNT(*) FROM grabs').fetchone()[0]==1


def test_success_timestamp_survives_history_expiration_and_later_retention_change(tmp_path):
    s=Store(str(tmp_path/'d.db'))
    wid=s.save_watch(watch_payload())
    add_match(s,wid,'completed',15)
    with s.conn() as db: db.execute('UPDATE watches SET enabled=0,grab_count=1 WHERE id=?',(wid,))
    result=s.prune_history(14,0)
    assert result['matches']==1 and s.watch(wid)
    assert s.watch(wid)['completed_success_at'] is not None
    result=s.prune_history(0,7)
    assert result['watches']==1


def test_disabled_retention_leaves_records(tmp_path):
    s=Store(str(tmp_path/'d.db'));wid=s.save_watch(watch_payload());add_match(s,wid,'completed',100)
    assert s.prune_history(0,0)==dict(events=0,matches=0,watches=0)


def test_event_pages_across_more_than_old_80_limit(tmp_path):
    s=Store(str(tmp_path/'d.db'))
    with s.conn() as db:
        for n in range(115): db.execute('INSERT INTO events(type,message,created_at) VALUES(?,?,?)',('test',str(n),'2026-11-02T00:00:00+00:00'))
    a=s.paged_events(1,30);b=s.paged_events(4,30)
    assert a['total']==115 and a['pages']==4
    assert len(a['items'])==30 and len(b['items'])==25
    assert a['items'][0]['message']=='114'
    assert b['items'][-1]['message']=='0'


def test_events_endpoint_session_and_csrf():
    # app.main is also imported in test_web.py with isolated test env.
    from fastapi.testclient import TestClient
    from app.main import app, PASSWORD, store
    with TestClient(app) as client:
        assert client.get('/api/events').status_code==401
        r=client.post('/api/login',json={'password':PASSWORD})
        assert r.status_code==200
        assert client.get('/api/events?page=0').status_code==422
        assert client.get('/api/events?page_size=1000').status_code==422
        store.log('test','entry for paginated activity')
        response=client.get('/api/events?page=1&page_size=2')
        assert response.status_code==200
        data=response.json()
        assert data['page']==1 and data['page_size']==2
        assert data['total'] >= 1 and isinstance(data['items'],list)


def test_pushover_payload_is_redacted_even_for_original_message(tmp_path):
    import asyncio
    import httpx
    from app.core import Engine
    sent=[]
    def fake(request):
        sent.append(request.content.decode())
        return httpx.Response(200,json={'status':1})
    settings=dict(pushover_key='test-app',pushover_user='test-user',
                  notification_types=['found','queued','completed','failed'],
                  pushover_privacy='watch_only')
    engine=Engine(Store(str(tmp_path/'push.db')), settings)
    async def run():
        engine.client=httpx.AsyncClient(transport=httpx.MockTransport(fake))
        await engine.push('NZB found','Secret.Search.Term: Secret.Release.File.1080p','found','Example Watch')
        settings['pushover_privacy']='generic'
        await engine.push('NZBWatch search summary','Secret.Search.Term: 20 titles: Secret.Release.File.1080p','found','Example Watch')
        await engine.client.aclose()
    asyncio.run(run())
    assert len(sent)==2
    assert 'Example+Watch' in sent[0]
    assert 'Example+Watch' not in sent[1]
    assert all('Secret' not in body and '1080p' not in body for body in sent)
