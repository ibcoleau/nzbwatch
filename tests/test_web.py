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
