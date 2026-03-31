"""LinkedIn publishing client for organization and member posts."""

from __future__ import annotations

import mimetypes
import os
import time
from urllib.parse import quote

import requests

REQUEST_TIMEOUT = 30
LINKEDIN_VERSION = os.getenv('LINKEDIN_API_VERSION', '202501')


class LinkedInClient:
    def __init__(self):
        self.access_token = os.getenv('LINKEDIN_ACCESS_TOKEN', '').strip()
        self.author_urn = os.getenv('LINKEDIN_AUTHOR_URN', '').strip()
        self.base_url = 'https://api.linkedin.com'

    def is_configured(self) -> bool:
        return bool(self.access_token and self.author_urn)

    def verify_credentials(self) -> dict:
        if not self.access_token:
            return {'configured': False, 'error': 'Access token not set'}
        if not self.author_urn:
            return {'configured': False, 'error': 'Author URN not set'}
        if not self.author_urn.startswith(('urn:li:organization:', 'urn:li:person:')):
            return {'configured': False, 'error': 'Author URN must start with urn:li:organization: or urn:li:person:'}

        author_type = 'organization' if self.author_urn.startswith('urn:li:organization:') else 'person'
        return {
            'configured': True,
            'status': 'ready',
            'author_urn': self.author_urn,
            'author_type': author_type,
        }

    def post_text(self, message: str) -> dict:
        if not self.is_configured():
            raise ValueError('LinkedIn credentials not configured')
        if not message.strip():
            raise ValueError('Message cannot be empty')

        response = self._request(
            'POST',
            '/rest/posts',
            json=self._post_payload(message=message),
            headers={'Content-Type': 'application/json'},
        )
        post_urn = response.headers.get('x-restli-id') or self._json_value(response, 'id')
        return {
            'success': True,
            'post_id': post_urn,
            'url': self._post_url(post_urn),
        }

    def post_image(self, image_url: str, message: str = '') -> dict:
        if not self.is_configured():
            raise ValueError('LinkedIn credentials not configured')
        if not image_url:
            raise ValueError('image_url is required')

        image_bytes, content_type = self._download_image(image_url)
        image_urn = self._upload_image(image_bytes, content_type)

        response = self._request(
            'POST',
            '/rest/posts',
            json=self._post_payload(
                message=message,
                media={'id': image_urn, 'title': self._media_title(message)},
            ),
            headers={'Content-Type': 'application/json'},
        )
        post_urn = response.headers.get('x-restli-id') or self._json_value(response, 'id')
        return {
            'success': True,
            'post_id': post_urn,
            'image_urn': image_urn,
            'url': self._post_url(post_urn),
        }

    def _api_headers(self, extra: dict | None = None) -> dict[str, str]:
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Linkedin-Version': LINKEDIN_VERSION,
            'X-Restli-Protocol-Version': '2.0.0',
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs):
        headers = self._api_headers(kwargs.pop('headers', None))
        response = requests.request(
            method,
            f'{self.base_url}{path}',
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        if response.status_code >= 400:
            raise Exception(self._error_message(response))
        return response

    def _download_image(self, image_url: str) -> tuple[bytes, str]:
        response = requests.get(image_url, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            raise Exception(f'LinkedIn image download failed: {response.status_code}')

        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip()
        if not content_type:
            guessed, _ = mimetypes.guess_type(image_url)
            content_type = guessed or 'image/png'

        return response.content, content_type

    def _upload_image(self, image_bytes: bytes, content_type: str) -> str:
        init_response = self._request(
            'POST',
            '/rest/images?action=initializeUpload',
            json={'initializeUploadRequest': {'owner': self.author_urn}},
            headers={'Content-Type': 'application/json'},
        )
        init_payload = init_response.json().get('value', {})
        upload_url = init_payload.get('uploadUrl')
        image_urn = init_payload.get('image')

        if not upload_url or not image_urn:
            raise Exception('LinkedIn image upload initialization failed')

        upload_response = requests.put(
            upload_url,
            data=image_bytes,
            headers={
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': content_type,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if upload_response.status_code >= 400:
            raise Exception(self._error_message(upload_response))

        self._wait_for_image(image_urn)
        return image_urn

    def _wait_for_image(self, image_urn: str) -> None:
        encoded_urn = quote(image_urn, safe='')
        for _ in range(5):
            response = self._request('GET', f'/rest/images/{encoded_urn}')
            payload = response.json()
            if payload.get('downloadUrl') or payload.get('status') in {'AVAILABLE', 'ALLOWED'}:
                return
            time.sleep(1)

    def _post_payload(self, *, message: str, media: dict | None = None) -> dict:
        payload = {
            'author': self.author_urn,
            'commentary': message,
            'visibility': 'PUBLIC',
            'distribution': {
                'feedDistribution': 'MAIN_FEED',
                'targetEntities': [],
                'thirdPartyDistributionChannels': [],
            },
            'lifecycleState': 'PUBLISHED',
            'isReshareDisabledByAuthor': False,
        }
        if media:
            payload['content'] = {'media': media}
        return payload

    def _media_title(self, message: str) -> str:
        normalized = ' '.join(message.split())
        return (normalized[:77] + '...') if len(normalized) > 80 else normalized or 'LinkedIn post image'

    def _json_value(self, response, key: str):
        try:
            return response.json().get(key)
        except ValueError:
            return None

    def _post_url(self, post_urn: str | None) -> str | None:
        if not post_urn:
            return None
        return f'https://www.linkedin.com/feed/update/{post_urn}'

    def _error_message(self, response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text or 'LinkedIn request failed'

        if isinstance(data, dict):
            message = (
                data.get('message')
                or data.get('error_description')
                or data.get('error')
                or data.get('serviceErrorMessage')
            )
            if message:
                return message
        return response.text or 'LinkedIn request failed'
