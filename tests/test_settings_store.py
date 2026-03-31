import importlib


def test_settings_store_encrypts_and_rehydrates_provider_values(monkeypatch, tmp_path):
    monkeypatch.setenv('SOCIAL_KANBAN_SETTINGS_PATH', str(tmp_path / 'settings.enc'))
    monkeypatch.setenv('SOCIAL_KANBAN_SETTINGS_KEY_PATH', str(tmp_path / 'settings.key'))
    monkeypatch.delenv('TWITTER_API_KEY', raising=False)

    module = importlib.import_module('core.settings_store')
    module = importlib.reload(module)

    store = module.SettingsStore()
    saved = store.save({
        'providers': {
            'twitter': {
                'TWITTER_API_KEY': 'key-1',
                'TWITTER_API_SECRET': 'secret-1',
                'TWITTER_ACCESS_TOKEN': 'token-1',
                'TWITTER_ACCESS_SECRET': 'access-secret-1',
            },
            'linkedin': {
                'LINKEDIN_ACCESS_TOKEN': 'linkedin-token',
            },
        },
    })

    assert saved['providers']['twitter']['TWITTER_API_KEY'] == 'key-1'
    assert store.load()['providers']['twitter']['TWITTER_ACCESS_TOKEN'] == 'token-1'
    assert store.has_any_credentials() is True
    assert store.store_path.exists()
    assert store.key_path.exists()

    store.apply_to_env()
    assert module.os.environ['TWITTER_API_KEY'] == 'key-1'
    assert module.os.environ['LINKEDIN_ACCESS_TOKEN'] == 'linkedin-token'
