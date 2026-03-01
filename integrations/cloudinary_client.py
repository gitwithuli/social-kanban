"""Cloudinary image upload client."""
import os
import hashlib
import time
import logging
from datetime import datetime, timedelta, UTC
import requests

logger = logging.getLogger(__name__)

# Request timeout in seconds
REQUEST_TIMEOUT = 30


class CloudinaryClient:
    """Client for uploading images to Cloudinary."""

    def __init__(self):
        self.cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME')
        self.api_key = os.getenv('CLOUDINARY_API_KEY')
        self.api_secret = os.getenv('CLOUDINARY_API_SECRET')
        self.upload_url = f'https://api.cloudinary.com/v1_1/{self.cloud_name}/image/upload'

    def is_configured(self):
        """Check if Cloudinary credentials are configured."""
        return bool(self.cloud_name and self.api_key and self.api_secret)

    def _generate_signature(self, params):
        """Generate signature for authenticated upload."""
        sorted_params = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
        to_sign = sorted_params + self.api_secret
        return hashlib.sha1(to_sign.encode()).hexdigest()

    def upload_base64(self, base64_data, folder='social-kanban', public_id=None):
        """Upload a base64 encoded image to Cloudinary.

        Args:
            base64_data: Base64 string (with or without data URI prefix)
            folder: Folder name in Cloudinary
            public_id: Optional custom public ID for the image

        Returns:
            dict with secure_url and public_id
        """
        if not self.is_configured():
            raise ValueError("Cloudinary credentials not configured")

        # Ensure proper data URI format
        if not base64_data.startswith('data:'):
            base64_data = f'data:image/png;base64,{base64_data}'

        timestamp = int(time.time())

        params = {
            'timestamp': timestamp,
            'folder': folder,
        }

        if public_id:
            params['public_id'] = public_id

        signature = self._generate_signature(params)

        payload = {
            'file': base64_data,
            'api_key': self.api_key,
            'signature': signature,
            **params
        }

        response = requests.post(self.upload_url, data=payload, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if 'error' in data:
            raise Exception(data['error'].get('message', 'Upload failed'))

        return {
            'success': True,
            'secure_url': data.get('secure_url'),
            'public_id': data.get('public_id'),
            'url': data.get('url')
        }

    def verify_credentials(self):
        """Verify Cloudinary credentials are valid."""
        if not self.is_configured():
            return {'configured': False, 'error': 'Credentials not set'}

        try:
            url = f'https://api.cloudinary.com/v1_1/{self.cloud_name}/resources/image'
            response = requests.get(url, auth=(self.api_key, self.api_secret), timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return {
                    'configured': True,
                    'status': 'ok',
                    'cloud_name': self.cloud_name
                }
            else:
                return {'configured': False, 'error': 'Invalid credentials'}
        except Exception as e:
            return {'configured': False, 'error': str(e)}

    def cleanup_old_images(self, folder='social-kanban', days=14, exclude=None):
        """Delete images older than specified days from a folder."""
        if not self.is_configured():
            return {'success': False, 'error': 'Credentials not configured'}

        if exclude is None:
            exclude = ['profile_picture', 'profile']

        cutoff_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        deleted = []
        skipped = []
        errors = []
        next_cursor = None

        try:
            while True:
                url = f'https://api.cloudinary.com/v1_1/{self.cloud_name}/resources/image/upload'
                params = {'prefix': folder, 'max_results': 100}
                if next_cursor:
                    params['next_cursor'] = next_cursor

                response = requests.get(url, params=params, auth=(self.api_key, self.api_secret), timeout=REQUEST_TIMEOUT)
                data = response.json()

                if 'error' in data:
                    return {'success': False, 'error': data['error'].get('message')}

                for resource in data.get('resources', []):
                    public_id = resource['public_id']

                    if any(exc in public_id for exc in exclude):
                        skipped.append(public_id)
                        continue

                    created_at = datetime.fromisoformat(resource['created_at'].replace('Z', '+00:00'))
                    created_at = created_at.replace(tzinfo=None)

                    if created_at < cutoff_date:
                        delete_result = self._delete_resource(public_id)
                        if delete_result:
                            deleted.append(public_id)
                        else:
                            errors.append(public_id)

                next_cursor = data.get('next_cursor')
                if not next_cursor:
                    break

            return {
                'success': True,
                'deleted_count': len(deleted),
                'deleted': deleted,
                'skipped_count': len(skipped),
                'skipped': skipped,
                'errors': errors
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _delete_resource(self, public_id):
        """Delete a single resource by public_id."""
        try:
            timestamp = int(time.time())
            params = {'public_id': public_id, 'timestamp': timestamp}
            signature = self._generate_signature(params)

            url = f'https://api.cloudinary.com/v1_1/{self.cloud_name}/image/destroy'
            payload = {
                'public_id': public_id,
                'api_key': self.api_key,
                'timestamp': timestamp,
                'signature': signature
            }

            response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            data = response.json()
            return data.get('result') == 'ok'
        except Exception as e:
            logger.warning(f"Failed to delete resource {public_id}: {e}")
            return False
