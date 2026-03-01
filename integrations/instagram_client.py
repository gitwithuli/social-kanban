"""Instagram Graph API posting client."""
import os
import time
import requests

# Request timeout in seconds
REQUEST_TIMEOUT = 30


class InstagramClient:
    """Client for posting to Instagram via Facebook Graph API."""

    def __init__(self):
        self.page_id = os.getenv('FACEBOOK_PAGE_ID')
        self.page_token = os.getenv('FACEBOOK_PAGE_TOKEN')
        self.instagram_account_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
        self.api_version = 'v24.0'
        self.base_url = f'https://graph.facebook.com/{self.api_version}'

    def is_configured(self):
        """Check if Instagram credentials are configured."""
        return bool(self.page_token and self.instagram_account_id)

    def _get_instagram_account_id(self):
        """Fetch Instagram Business Account ID from Facebook Page."""
        if self.instagram_account_id:
            return self.instagram_account_id

        if not self.page_id or not self.page_token:
            return None

        url = f'{self.base_url}/{self.page_id}'
        params = {
            'fields': 'instagram_business_account',
            'access_token': self.page_token
        }

        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'instagram_business_account' in data:
            return data['instagram_business_account'].get('id')
        return None

    def post_image(self, image_url, caption=''):
        """Post an image to Instagram.

        Instagram Graph API requires a two-step process:
        1. Create a media container with the image URL
        2. Publish the container

        Note: image_url must be publicly accessible.
        """
        if not self.is_configured():
            raise ValueError("Instagram credentials not configured")

        ig_account_id = self._get_instagram_account_id()
        if not ig_account_id:
            raise ValueError("Instagram Business Account not linked to Facebook Page")

        # Step 1: Create media container
        container_url = f'{self.base_url}/{ig_account_id}/media'
        container_payload = {
            'image_url': image_url,
            'caption': caption,
            'access_token': self.page_token
        }

        container_response = requests.post(container_url, data=container_payload, timeout=REQUEST_TIMEOUT)
        container_data = container_response.json()

        if 'error' in container_data:
            raise Exception(container_data['error'].get('message', 'Unknown error creating media container'))

        container_id = container_data.get('id')
        if not container_id:
            raise Exception('Failed to create media container')

        # Step 2: Wait for container to be ready and publish
        # Instagram needs time to process the image
        max_attempts = 10
        for attempt in range(max_attempts):
            status_url = f'{self.base_url}/{container_id}'
            status_params = {
                'fields': 'status_code',
                'access_token': self.page_token
            }
            status_response = requests.get(status_url, params=status_params, timeout=REQUEST_TIMEOUT)
            status_data = status_response.json()

            status_code = status_data.get('status_code')
            if status_code == 'FINISHED':
                break
            elif status_code == 'ERROR':
                raise Exception('Media processing failed')

            time.sleep(1)

        # Step 3: Publish the container
        publish_url = f'{self.base_url}/{ig_account_id}/media_publish'
        publish_payload = {
            'creation_id': container_id,
            'access_token': self.page_token
        }

        publish_response = requests.post(publish_url, data=publish_payload, timeout=REQUEST_TIMEOUT)
        publish_data = publish_response.json()

        if 'error' in publish_data:
            raise Exception(publish_data['error'].get('message', 'Unknown error publishing'))

        media_id = publish_data.get('id')
        return {
            'success': True,
            'post_id': media_id,
            'url': f"https://instagram.com/p/{media_id}"
        }

    def verify_credentials(self):
        """Verify the Instagram connection is valid."""
        if not self.page_token:
            return {'configured': False, 'error': 'Page token not set'}

        ig_account_id = self._get_instagram_account_id()
        if not ig_account_id:
            return {'configured': False, 'error': 'Instagram Business Account not linked'}

        # Get Instagram account info
        url = f'{self.base_url}/{ig_account_id}'
        params = {
            'fields': 'username,name,profile_picture_url',
            'access_token': self.page_token
        }

        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'error' in data:
            return {'configured': False, 'error': data['error'].get('message')}

        return {
            'configured': True,
            'status': 'ok',
            'instagram_id': ig_account_id,
            'username': data.get('username'),
            'name': data.get('name')
        }
