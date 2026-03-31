"""LinkedIn API helper for lightweight connection checks."""

from __future__ import annotations

import os

import requests

REQUEST_TIMEOUT = 30
LINKEDIN_VERSION = os.getenv('LINKEDIN_API_VERSION', '202501')


class LinkedInClient:
    def __init__(self):
        self.access_token = os.getenv('LINKEDIN_ACCESS_TOKEN')
        self.author_urn = os.getenv('LINKEDIN_AUTHOR_URN')
        self.base_url = 'https://api.linkedin.com'

    def is_configured(self) -> bool:
        return bool(self.access_token)

    def verify_credentials(self) -> dict:
        if not self.is_configured():
            return {'configured': False, 'error': 'Access token not set'}

        response = requests.get(
            f'{self.base_url}/v2/userinfo',
            headers={
                'Authorization': f'Bearer {self.access_token}',
                'LinkedIn-Version': LINKEDIN_VERSION,
            },
            timeout=REQUEST_TIMEOUT,
        )

        try:
            data = response.json()
        except ValueError:
            data = {'message': response.text}

        if response.status_code >= 400:
            return {'configured': False, 'error': data.get('message') or data.get('error_description') or 'LinkedIn authentication failed'}

        return {
            'configured': True,
            'status': 'ok',
            'sub': data.get('sub'),
            'name': data.get('name'),
            'email': data.get('email'),
            'author_urn': self.author_urn,
        }
