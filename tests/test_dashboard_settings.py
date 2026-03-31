import importlib


EDITABLE_PROVIDER_KEYS = [
    'ANTHROPIC_API_KEY',
    'GROQ_API_KEY',
    'TWITTER_API_KEY',
    'TWITTER_API_SECRET',
    'TWITTER_ACCESS_TOKEN',
    'TWITTER_ACCESS_SECRET',
    'TWITTER_BEARER_TOKEN',
    'FACEBOOK_PAGE_ID',
    'FACEBOOK_PAGE_TOKEN',
    'INSTAGRAM_ACCOUNT_ID',
    'LINKEDIN_ACCESS_TOKEN',
    'LINKEDIN_AUTHOR_URN',
    'CLOUDINARY_CLOUD_NAME',
    'CLOUDINARY_API_KEY',
    'CLOUDINARY_API_SECRET',
]


def _load_dashboard(monkeypatch, tmp_path, *, application_root=''):
    monkeypatch.setenv('SOCIAL_KANBAN_SETTINGS_PATH', str(tmp_path / 'settings.enc'))
    monkeypatch.setenv('SOCIAL_KANBAN_SETTINGS_KEY_PATH', str(tmp_path / 'settings.key'))
    monkeypatch.setenv('SOCIAL_KANBAN_FLASK_SECRET_PATH', str(tmp_path / 'flask-secret.txt'))
    monkeypatch.setenv('SOCIAL_KANBAN_AGENT_TOKEN_PATH', str(tmp_path / 'agent-token.txt'))
    monkeypatch.setenv('DATABASE_URL', f"sqlite:///{tmp_path / 'social-kanban.db'}")
    monkeypatch.setenv('SOCIAL_KANBAN_SEED_SAMPLE_DATA', '0')
    monkeypatch.delenv('DASHBOARD_PASSWORD', raising=False)
    if application_root:
        monkeypatch.setenv('APPLICATION_ROOT', application_root)
    else:
        monkeypatch.delenv('APPLICATION_ROOT', raising=False)
    for key in EDITABLE_PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    settings_store = importlib.import_module('core.settings_store')
    importlib.reload(settings_store)
    dashboard = importlib.import_module('dashboard')
    return importlib.reload(dashboard)


def test_first_run_shows_onboarding(monkeypatch, tmp_path):
    dashboard = _load_dashboard(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    response = client.get('/')

    assert response.status_code == 200
    assert b'Connect your socials before you start filling the board.' in response.data


def test_agent_hook_creates_posts_for_multiple_platforms(monkeypatch, tmp_path):
    dashboard = _load_dashboard(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    save_response = client.post('/api/settings', json={
        'providers': {
            'anthropic': {
                'ANTHROPIC_API_KEY': 'sk-ant-test',
            },
        },
    })
    assert save_response.status_code == 200

    response = client.post('/api/posts', json={
        'content': 'Ekuri can queue platform-specific drafts from the agent.',
        'platform': ['twitter', 'linkedin'],
        'scheduled_at': '2026-04-01T15:00:00Z',
    })

    assert response.status_code == 201
    payload = response.get_json()
    assert payload['success'] is True
    assert [post['platform'] for post in payload['posts']] == ['twitter', 'linkedin']


def test_settings_route_works_under_application_root(monkeypatch, tmp_path):
    dashboard = _load_dashboard(monkeypatch, tmp_path, application_root='/kanban')
    client = dashboard.app.test_client()

    response = client.get('/kanban/settings')

    assert response.status_code == 200
    assert b'Provider Settings' in response.data


def test_dashboard_uses_draft_review_columns(monkeypatch, tmp_path):
    dashboard = _load_dashboard(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    save_response = client.post('/api/settings', json={
        'providers': {
            'anthropic': {
                'ANTHROPIC_API_KEY': 'sk-ant-test',
            },
        },
    })
    assert save_response.status_code == 200

    create_response = client.post('/api/posts', json={
        'content': 'Queue this for review before publishing.',
        'platform': ['twitter'],
    })
    assert create_response.status_code == 201

    response = client.get('/')

    assert response.status_code == 200
    assert b'New Draft' in response.data
    assert b'Pending Review' in response.data
    assert b'Scheduled' in response.data
    assert b'Fresh Quotes' not in response.data
