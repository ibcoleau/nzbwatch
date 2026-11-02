import os
import tempfile
from pathlib import Path

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='nzbwatch-web-test-')
os.environ['ADMIN_PASSWORD'] = 'a-test-password-at-least-12-characters'
os.environ['APP_SECRET'] = 'test-secret-signing-key-very-long-at-least-32-chars-1234'

from fastapi.testclient import TestClient
from app.main import app


def test_web_login_csrf_watchlist():
    with TestClient(app) as c:
        assert c.get('/api/session').json()['authenticated'] is False
        assert c.get('/api/state').status_code == 401
        assert c.post('/api/login',json={'password':'wrong'}).status_code == 401
        r=c.post('/api/login',json={'password':os.environ['ADMIN_PASSWORD']})
        assert r.status_code == 200
        assert 'httponly' in r.headers['set-cookie'].lower()
        csrf=r.json()['csrf']
        w={'name':'Example','query':'Example 1080p','mode':'notify','max_grabs':1}
        assert c.post('/api/watches',json=w).status_code == 403
        a=c.post('/api/watches',json=w,headers={'X-CSRF-Token':csrf})
        assert a.status_code == 201, a.text
        wid=a.json()['id']
        assert c.get('/api/state').json()['counts']['watches'] >= 1
        export=c.get('/api/export').json()
        assert export['watches'][0]['query']=='Example 1080p'
        assert 'password' not in str(export).lower()
        assert c.delete(f'/api/watches/{wid}',headers={'X-CSRF-Token':csrf}).status_code==200
        assert c.post('/api/logout',headers={'X-CSRF-Token':csrf}).status_code==200
        assert c.get('/api/state').status_code==401


def test_web_settings_are_protected_and_hot_applied():
    from app.main import settings, CONFIG
    with TestClient(app) as c:
        assert c.get('/api/settings').status_code == 401
        r=c.post('/api/login',json={'password':os.environ['ADMIN_PASSWORD']})
        csrf=r.json()['csrf']
        data=c.get('/api/settings').json()
        assert 'hydra_key' not in data
        assert 'sab_key' not in data
        assert data['configured_secrets']['hydra_key'] is False
        assert c.put('/api/settings',json=data).status_code==403
        data['hydra_url']='http://example.local:5076/nzbhydra2'
        data['hydra_key']='new-test-key'
        data['scan_interval_minutes']=9
        response=c.put('/api/settings',json=data,headers={'X-CSRF-Token':csrf})
        assert response.status_code==200,response.text
        assert response.json()['configured_secrets']['hydra_key'] is True
        assert 'new-test-key' not in response.text
        assert CONFIG['hydra_key']=='new-test-key'
        assert settings.path.exists()
        preview=c.get('/api/query-preview?q=My.Series.S01E02.1080p.WEB-DL-GROUP')
        assert preview.status_code==200
        assert preview.json()['search']=='My Series S01E02'
        data=c.get('/api/settings').json()
        assert data['hydra_url']=='http://example.local:5076/nzbhydra2'
        assert data['scan_interval_minutes']==9
        assert 'new-test-key' not in str(data)
