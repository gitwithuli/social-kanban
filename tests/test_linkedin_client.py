import importlib


class FakeResponse:
    def __init__(self, status_code=200, *, json_data=None, headers=None, text='', content=b''):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.text = text
        self.content = content

    def json(self):
        return self._json_data


def _load_module(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f"sqlite:///{tmp_path / 'social-kanban.db'}")
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'linkedin-token')
    monkeypatch.setenv('LINKEDIN_AUTHOR_URN', 'urn:li:organization:112573922')
    module = importlib.import_module('integrations.linkedin_client')
    return importlib.reload(module)


def test_verify_credentials_requires_author_urn(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f"sqlite:///{tmp_path / 'social-kanban.db'}")
    monkeypatch.setenv('LINKEDIN_ACCESS_TOKEN', 'linkedin-token')
    monkeypatch.delenv('LINKEDIN_AUTHOR_URN', raising=False)

    module = importlib.import_module('integrations.linkedin_client')
    module = importlib.reload(module)

    result = module.LinkedInClient().verify_credentials()

    assert result == {'configured': False, 'error': 'Author URN not set'}


def test_post_text_uses_posts_api(monkeypatch, tmp_path):
    module = _load_module(monkeypatch, tmp_path)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured['method'] = method
        captured['url'] = url
        captured['kwargs'] = kwargs
        return FakeResponse(status_code=201, headers={'x-restli-id': 'urn:li:share:123'})

    monkeypatch.setattr(module.requests, 'request', fake_request)

    result = module.LinkedInClient().post_text('Ship the launch note.')

    assert captured['method'] == 'POST'
    assert captured['url'] == 'https://api.linkedin.com/rest/posts'
    assert captured['kwargs']['json']['author'] == 'urn:li:organization:112573922'
    assert captured['kwargs']['json']['commentary'] == 'Ship the launch note.'
    assert captured['kwargs']['json']['lifecycleState'] == 'PUBLISHED'
    assert captured['kwargs']['headers']['Authorization'] == 'Bearer linkedin-token'
    assert captured['kwargs']['headers']['Linkedin-Version'] == module.LINKEDIN_VERSION
    assert result == {
        'success': True,
        'post_id': 'urn:li:share:123',
        'url': 'https://www.linkedin.com/feed/update/urn:li:share:123',
    }


def test_post_image_uploads_asset_then_creates_post(monkeypatch, tmp_path):
    module = _load_module(monkeypatch, tmp_path)
    captured = {'requests': [], 'puts': [], 'downloads': []}

    def fake_request(method, url, **kwargs):
        captured['requests'].append((method, url, kwargs))
        if url.endswith('/rest/images?action=initializeUpload'):
            return FakeResponse(
                json_data={
                    'value': {
                        'uploadUrl': 'https://upload.linkedin.test/image',
                        'image': 'urn:li:image:abc123',
                    }
                }
            )
        if '/rest/images/' in url:
            return FakeResponse(json_data={'downloadUrl': 'https://cdn.linkedin.test/image.png'})
        if url.endswith('/rest/posts'):
            return FakeResponse(status_code=201, headers={'x-restli-id': 'urn:li:share:456'})
        raise AssertionError(f'unexpected request: {method} {url}')

    def fake_get(url, **kwargs):
        captured['downloads'].append((url, kwargs))
        return FakeResponse(
            status_code=200,
            headers={'Content-Type': 'image/png'},
            content=b'image-bytes',
        )

    def fake_put(url, **kwargs):
        captured['puts'].append((url, kwargs))
        return FakeResponse(status_code=201)

    monkeypatch.setattr(module.requests, 'request', fake_request)
    monkeypatch.setattr(module.requests, 'get', fake_get)
    monkeypatch.setattr(module.requests, 'put', fake_put)

    result = module.LinkedInClient().post_image('https://res.cloudinary.com/demo/image.png', 'Launch visual')

    assert captured['downloads'][0][0] == 'https://res.cloudinary.com/demo/image.png'
    assert captured['puts'][0][0] == 'https://upload.linkedin.test/image'
    assert captured['puts'][0][1]['data'] == b'image-bytes'
    assert captured['puts'][0][1]['headers']['Content-Type'] == 'image/png'
    post_payload = captured['requests'][-1][2]['json']
    assert post_payload['content']['media']['id'] == 'urn:li:image:abc123'
    assert post_payload['commentary'] == 'Launch visual'
    assert result == {
        'success': True,
        'post_id': 'urn:li:share:456',
        'image_urn': 'urn:li:image:abc123',
        'url': 'https://www.linkedin.com/feed/update/urn:li:share:456',
    }
