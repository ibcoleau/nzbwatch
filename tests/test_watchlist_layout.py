"""v0.2.3 regression tests for persisted layout and completed-watch segregation."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from app.core import Store
from app.settings import SettingsFile


def make_watch(name, grabs=1):
    return {
        'name': name, 'query': 'Example Series 2026', 'enabled': True,
        'mode': 'notify', 'match_mode': 'all', 'required_words': '',
        'excluded_words': '', 'categories': '', 'sab_category': '',
        'min_mb': None, 'max_mb': None, 'max_age_days': None,
        'max_grabs': grabs, 'poll_minutes': 0, 'broad_search': True,
        'resolution_mode': 'inherit',
    }


def test_layout_defaults_and_survives_restart(tmp_path):
    path = tmp_path / 'config.json'
    settings = SettingsFile(str(path))
    assert settings.public()['watchlist_columns'] == 2
    settings.update({'watchlist_columns': 1})
    assert json.loads(path.read_text())['watchlist_columns'] == 1
    assert SettingsFile(str(path)).public()['watchlist_columns'] == 1
    settings.update({'watchlist_columns': 2})
    assert SettingsFile(str(path)).public()['watchlist_columns'] == 2


def test_old_config_missing_layout_migrates_without_renaming_keys(tmp_path):
    path = tmp_path / 'config.json'
    original = SettingsFile(str(path))
    original.update({'scan_interval_minutes': 43, 'sab_key': 'sample-key'})
    saved = json.loads(path.read_text())
    del saved['watchlist_columns']
    path.write_text(json.dumps(saved))
    upgraded = SettingsFile(str(path))
    assert upgraded.public()['watchlist_columns'] == 2
    assert upgraded.public()['scan_interval_minutes'] == 43
    assert upgraded.data['sab_key'] == 'sample-key'
    upgraded.update({'watchlist_columns': 1})
    assert json.loads(path.read_text())['watchlist_columns'] == 1


def test_invalid_layout_does_not_write_config(tmp_path):
    settings = SettingsFile(str(tmp_path / 'config.json'))
    before = settings.path.read_bytes()
    for invalid in (0, 3, 1.5, '1', False, None):
        with pytest.raises(ValueError):
            settings.update({'watchlist_columns': invalid})
        assert settings.path.read_bytes() == before


def test_completed_and_paused_watches_retain_editable_records(tmp_path):
    db = Store(str(tmp_path / 'db.sqlite3'))
    active = db.save_watch(make_watch('Active'))
    done = db.save_watch(make_watch('Completed'))
    paused = db.save_watch(make_watch('Paused', 0))
    with db.conn() as con:
        con.execute('UPDATE watches SET enabled=0, grab_count=1 WHERE id=?', (done,))
        con.execute('UPDATE watches SET enabled=0 WHERE id=?', (paused,))
    watches = db.watches()
    by_id = {w['id']: w for w in watches}
    assert by_id[active]['enabled'] == 1
    assert by_id[done]['enabled'] == 0 and by_id[done]['grab_count'] >= by_id[done]['max_grabs']
    assert by_id[paused]['enabled'] == 0 and by_id[paused]['max_grabs'] == 0
    # Completion is a presentation-level grouping and must not move or delete
    # any SQLite records, matches, or history.
    assert len(watches) == 3
    assert db.watch(done)['name'] == 'Completed'
    db.save_watch({**make_watch('Reactivated'), 'max_grabs': 3}, done)
    assert db.watch(done)['grab_count'] == 1
    assert db.watch(done)['enabled'] == 1


def test_web_settings_state_includes_preference():
    from fastapi.testclient import TestClient
    # Other web tests initialize app with this same DATA_DIR; don't mutate
    # persistent tests' state by relying on the module's default.
    from app.main import app, CONFIG
    with TestClient(app) as client:
        session = client.post('/api/login', json={'password': os.environ['ADMIN_PASSWORD']})
        assert session.status_code == 200
        token = session.json()['csrf']
        data = client.get('/api/settings').json()
        data['watchlist_columns'] = 1
        response = client.put('/api/settings', json=data, headers={'X-CSRF-Token': token})
        assert response.status_code == 200, response.text
        assert client.get('/api/state').json()['preferences']['watchlist_columns'] == 1
        assert CONFIG['watchlist_columns'] == 1
        data['watchlist_columns'] = 2
        assert client.put('/api/settings', json=data, headers={'X-CSRF-Token': token}).status_code == 200
