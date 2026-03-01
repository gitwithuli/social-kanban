"""Facebook Page posting client."""
import os
import requests

# Request timeout in seconds
REQUEST_TIMEOUT = 30


class FacebookClient:
    """Client for posting to Facebook Pages."""

    def __init__(self):
        self.page_id = os.getenv('FACEBOOK_PAGE_ID')
        self.page_token = os.getenv('FACEBOOK_PAGE_TOKEN')
        self.api_version = 'v24.0'
        self.base_url = f'https://graph.facebook.com/{self.api_version}'

    def is_configured(self):
        """Check if Facebook credentials are configured."""
        return bool(self.page_id and self.page_token)

    def post_text(self, message):
        """Post a text message to the Facebook Page."""
        if not self.is_configured():
            raise ValueError("Facebook credentials not configured")

        url = f'{self.base_url}/{self.page_id}/feed'
        payload = {
            'message': message,
            'access_token': self.page_token
        }

        response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'error' in data:
            raise Exception(data['error'].get('message', 'Unknown error'))

        return {
            'success': True,
            'post_id': data.get('id'),
            'url': f"https://facebook.com/{data.get('id')}"
        }

    def post_image(self, image_url, message=''):
        """Post an image to the Facebook Page."""
        if not self.is_configured():
            raise ValueError("Facebook credentials not configured")

        url = f'{self.base_url}/{self.page_id}/photos'
        payload = {
            'url': image_url,
            'caption': message,
            'access_token': self.page_token
        }

        response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'error' in data:
            raise Exception(data['error'].get('message', 'Unknown error'))

        return {
            'success': True,
            'post_id': data.get('post_id') or data.get('id'),
            'url': f"https://facebook.com/{data.get('post_id') or data.get('id')}"
        }

    def verify_credentials(self):
        """Verify the Page token is valid."""
        if not self.is_configured():
            return {'configured': False, 'error': 'Credentials not set'}

        url = f'{self.base_url}/{self.page_id}'
        params = {
            'fields': 'name,id',
            'access_token': self.page_token
        }

        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'error' in data:
            return {'configured': False, 'error': data['error'].get('message')}

        return {
            'configured': True,
            'status': 'ok',
            'page_name': data.get('name'),
            'page_id': data.get('id')
        }
