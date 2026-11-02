"""Dismissal must hide orphaned jobs without allowing accidental re-downloads."""
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.core import Store, fingerprint, now


def make_watch(store):
    return store.save_watch({
        'name': 'Series watch', 'query': 'Example Show', 'enabled': True,
        'mode': 'auto', 'match_mode': 'all', 'required_words': '',
        'excluded_words': '', 'categories': '', 'sab_category': '',
        'min_mb': None, 'max_mb': None, 'max_age_days': None,
        'max_grabs': 0, 'poll_minutes': 30,
    })


def make_match(store, watch_id, *, status='queued', note=None, missing_since=None, nzo_id='SABnzbd_nzo_123'):
    title = f'Example Show S01E{watch_id:02d} 1080p'
    with store.conn() as db:
        cur = db.execute(
            'INSERT INTO matches(watch_id,title,fingerprint,link,status,nzo_id,note,'
            'sab_missing_since,found_at,queued_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (watch_id, title, fingerprint(title), 'http://hydra/getnzb/1', status,
             nzo_id, note, missing_since, now(), now()),
        )
        db.execute('INSERT INTO grabs(fingerprint,match_id,nzo_id,queued_at) VALUES(?,?,?,?)',
                   (fingerprint(title), cur.lastrowid, nzo_id or 'prior-nzo', now()))
        db.execute('UPDATE watches SET grab_count=1 WHERE id=?', (watch_id,))
        return cur.lastrowid


def test_dismiss_orphan_keeps_grab_ledger_and_watch_quota(tmp_path):
    store = Store(str(tmp_path / 'store.sqlite3'))
    wid = make_watch(store)
    mid = make_match(store, wid, note='SAB status unverified: job not found in current queue or recent/archived history.', missing_since=now())
    before = store.recent()
    assert before['counts']['queued'] == 1
    assert store.dismiss_orphaned_match(mid) == {'id': mid, 'dismissed': True}
    assert store.match(mid)['status'] == 'dismissed'
    assert store.recent()['matches'] == []
    assert store.recent()['counts']['queued'] == 0
    assert store.watch(wid)['grab_count'] == 1
    with store.conn() as db:
        assert db.execute('SELECT count(*) FROM grabs WHERE match_id=?', (mid,)).fetchone()[0] == 1
    assert any('Dismissed orphaned download' in ev['message'] for ev in store.recent()['events'])
    # Dismissal doesn't vanish from dedupe on a subsequent scan or restart.
    assert Store(store.path).match(mid)['status'] == 'dismissed'
    assert Store(store.path).recent()['matches'] == []
    with pytest.raises(ValueError):
        store.dismiss_orphaned_match(mid)


def test_active_queued_and_completed_cannot_be_dismissed(tmp_path):
    store = Store(str(tmp_path / 'store.sqlite3'))
    wid = make_watch(store)
    mid = make_match(store, wid, note=None)
    with pytest.raises(ValueError):
        store.dismiss_orphaned_match(mid)
    with store.conn() as db:
        db.execute("UPDATE matches SET note='SAB status unverified: temporary', sab_missing_since=NULL WHERE id=?", (mid,))
    with pytest.raises(ValueError):
        store.dismiss_orphaned_match(mid)
    with store.conn() as db:
        db.execute("UPDATE matches SET status='completed', sab_missing_since=? WHERE id=?", (now(), mid))
    with pytest.raises(ValueError):
        store.dismiss_orphaned_match(mid)
    assert store.match(mid)['status'] == 'completed'
    with pytest.raises(KeyError):
        store.dismiss_orphaned_match(mid + 1)


def test_assumed_completion_can_be_dismissed(tmp_path):
    store = Store(str(tmp_path / 'store.sqlite3'))
    wid = make_watch(store)
    mid = make_match(store, wid, status='assumed_completed', note='Completion assumed: no longer in SAB')
    assert store.recent()['counts']['completed'] == 1
    store.dismiss_orphaned_match(mid)
    assert store.recent()['counts']['completed'] == 0
    assert store.match(mid)['status'] == 'dismissed'


def test_dismiss_endpoint_requires_login_csrf_and_rejects_non_orphans():
    # app.main has module-level Store, using the existing per-test temporary DATA_DIR.
    from app.main import app, store
    with TestClient(app) as client:
        wid = make_watch(store)
        mid = make_match(store, wid, note='SAB status unverified: job missing', missing_since=now())
        try:
            assert client.delete(f'/api/matches/{mid}').status_code == 401
            login = client.post('/api/login', json={'password': os.environ['ADMIN_PASSWORD']})
            assert login.status_code == 200
            assert client.delete(f'/api/matches/{mid}').status_code == 403
            headers = {'X-CSRF-Token': login.json()['csrf']}
            assert client.delete('/api/matches/999999999', headers=headers).status_code == 404
            ok = client.delete(f'/api/matches/{mid}', headers=headers)
            assert ok.status_code == 200
            assert ok.json()['dismissed'] is True
            assert mid not in {m['id'] for m in client.get('/api/state').json()['matches']}
            assert client.delete(f'/api/matches/{mid}', headers=headers).status_code == 409
            assert client.post(f'/api/matches/{mid}/queue', headers=headers).json()['result'] == 'already_handled'
        finally:
            store.delete_watch(wid)


def test_ui_only_renders_remove_for_missing_jobs():
    base = Path(__file__).resolve().parents[1] / 'app/static/app.js'
    js = base.read_text()
    assert 'function canDismissOrphan(m)' in js
    assert "m.status==='queued'" in js
    assert "m.status==='assumed_completed'" in js
    assert 'data-dismiss-match' in js
    assert "method:'DELETE'" in js
