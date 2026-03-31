import importlib
from types import SimpleNamespace


def test_verify_credentials_returns_configured_payload(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f"sqlite:///{tmp_path / 'social-kanban.db'}")
    monkeypatch.setenv('TWITTER_API_KEY', 'consumer-key')
    monkeypatch.setenv('TWITTER_API_SECRET', 'consumer-secret')
    monkeypatch.setenv('TWITTER_ACCESS_TOKEN', 'access-token')
    monkeypatch.setenv('TWITTER_ACCESS_SECRET', 'access-secret')
    monkeypatch.delenv('TWITTER_BEARER_TOKEN', raising=False)

    module = importlib.import_module('integrations.twitter_client')
    module = importlib.reload(module)

    class FakeUser:
        screen_name = 'ekuriapp'
        id = 123456
        name = 'Ekuri App'

    class FakeAPI:
        def __init__(self, auth):
            self.auth = auth

        def verify_credentials(self, skip_status=True):
            assert skip_status is True
            return FakeUser()

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module.tweepy = SimpleNamespace(
        Client=FakeClient,
        OAuth1UserHandler=lambda *args: ('oauth1', args),
        API=FakeAPI,
    )

    client = module.TwitterClient(dry_run=False)
    result = client.verify_credentials()

    assert result == {
        'configured': True,
        'status': 'ok',
        'username': 'ekuriapp',
        'id': 123456,
        'name': 'Ekuri App',
    }
