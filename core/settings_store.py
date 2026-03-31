from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / 'data'

SOCIAL_POST_PROVIDERS = ('twitter', 'facebook', 'instagram', 'linkedin')

PROVIDER_DEFINITIONS: dict[str, dict[str, Any]] = {
    'anthropic': {
        'label': 'Anthropic',
        'description': 'Claude-powered drafting, rewriting, and planning.',
        'fields': [
            {'name': 'ANTHROPIC_API_KEY', 'label': 'API Key', 'placeholder': 'sk-ant-...', 'secret': True, 'required': True},
        ],
    },
    'groq': {
        'label': 'Groq',
        'description': 'Optional quote extraction and document parsing.',
        'fields': [
            {'name': 'GROQ_API_KEY', 'label': 'API Key', 'placeholder': 'gsk_...', 'secret': True, 'required': True},
        ],
    },
    'twitter': {
        'label': 'X / Twitter',
        'description': 'Tweet posting with full API credentials.',
        'fields': [
            {'name': 'TWITTER_API_KEY', 'label': 'API Key', 'placeholder': 'x api key', 'secret': True, 'required': True},
            {'name': 'TWITTER_API_SECRET', 'label': 'API Secret', 'placeholder': 'x api secret', 'secret': True, 'required': True},
            {'name': 'TWITTER_ACCESS_TOKEN', 'label': 'Access Token', 'placeholder': 'x access token', 'secret': True, 'required': True},
            {'name': 'TWITTER_ACCESS_SECRET', 'label': 'Access Secret', 'placeholder': 'x access secret', 'secret': True, 'required': True},
            {'name': 'TWITTER_BEARER_TOKEN', 'label': 'Bearer Token', 'placeholder': 'optional bearer token', 'secret': True, 'required': False},
        ],
    },
    'facebook': {
        'label': 'Facebook',
        'description': 'Facebook Page publishing via Graph API.',
        'fields': [
            {'name': 'FACEBOOK_PAGE_ID', 'label': 'Page ID', 'placeholder': 'facebook page id', 'secret': False, 'required': True},
            {'name': 'FACEBOOK_PAGE_TOKEN', 'label': 'Page Access Token', 'placeholder': 'facebook page token', 'secret': True, 'required': True},
        ],
    },
    'instagram': {
        'label': 'Instagram',
        'description': 'Instagram publishing via the Facebook Graph API.',
        'fields': [
            {'name': 'FACEBOOK_PAGE_ID', 'label': 'Facebook Page ID', 'placeholder': 'facebook page id', 'secret': False, 'required': False},
            {'name': 'FACEBOOK_PAGE_TOKEN', 'label': 'Page Access Token', 'placeholder': 'facebook page token', 'secret': True, 'required': True},
            {'name': 'INSTAGRAM_ACCOUNT_ID', 'label': 'Instagram Account ID', 'placeholder': 'instagram business account id', 'secret': False, 'required': False},
        ],
    },
    'linkedin': {
        'label': 'LinkedIn',
        'description': 'LinkedIn organization posting via the Posts API.',
        'fields': [
            {'name': 'LINKEDIN_ACCESS_TOKEN', 'label': 'Access Token', 'placeholder': 'linkedin access token', 'secret': True, 'required': True},
            {'name': 'LINKEDIN_AUTHOR_URN', 'label': 'Author URN', 'placeholder': 'urn:li:person:... or urn:li:organization:...', 'secret': False, 'required': True},
        ],
    },
    'cloudinary': {
        'label': 'Cloudinary',
        'description': 'Image hosting for Instagram and rich post workflows.',
        'fields': [
            {'name': 'CLOUDINARY_CLOUD_NAME', 'label': 'Cloud Name', 'placeholder': 'cloud name', 'secret': False, 'required': True},
            {'name': 'CLOUDINARY_API_KEY', 'label': 'API Key', 'placeholder': 'cloudinary api key', 'secret': True, 'required': True},
            {'name': 'CLOUDINARY_API_SECRET', 'label': 'API Secret', 'placeholder': 'cloudinary api secret', 'secret': True, 'required': True},
        ],
    },
}

EDITABLE_ENV_KEYS = tuple(
    sorted(
        {
            field['name']
            for provider in PROVIDER_DEFINITIONS.values()
            for field in provider['fields']
        }
    )
)

ENV_FALLBACKS = {key: os.environ.get(key) for key in EDITABLE_ENV_KEYS}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store_path() -> Path:
    return Path(os.getenv('SOCIAL_KANBAN_SETTINGS_PATH', str(DATA_DIR / 'settings.enc')))


def _default_key_path() -> Path:
    return Path(os.getenv('SOCIAL_KANBAN_SETTINGS_KEY_PATH', str(DATA_DIR / 'settings.key')))


def _default_secret_path(name: str) -> Path:
    env_name = f"SOCIAL_KANBAN_{name.upper().replace('-', '_')}_PATH"
    return Path(os.getenv(env_name, str(DATA_DIR / f'{name}.txt')))


def ensure_persistent_secret(env_key: str, file_name: str, *, bytes_length: int = 32) -> str:
    existing = os.getenv(env_key)
    if existing:
        return existing

    path = _default_secret_path(file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        secret = path.read_text(encoding='utf-8').strip()
        if secret:
            os.environ[env_key] = secret
            return secret

    secret = secrets.token_urlsafe(bytes_length)
    path.write_text(secret, encoding='utf-8')
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ[env_key] = secret
    return secret


class SettingsStore:
    def __init__(self, store_path: Path | None = None, key_path: Path | None = None):
        self.store_path = store_path or _default_store_path()
        self.key_path = key_path or _default_key_path()

    def _ensure_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            if key:
                return key

        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        try:
            self.key_path.chmod(0o600)
        except OSError:
            pass
        return key

    def _fernet(self) -> Fernet:
        return Fernet(self._ensure_key())

    def load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {'providers': {}, 'updated_at': None}

        try:
            payload = self._fernet().decrypt(self.store_path.read_bytes())
            data = json.loads(payload.decode('utf-8'))
        except (InvalidToken, json.JSONDecodeError, OSError):
            return {'providers': {}, 'updated_at': None}

        providers = data.get('providers') if isinstance(data, dict) else {}
        if not isinstance(providers, dict):
            providers = {}

        normalized: dict[str, dict[str, str]] = {}
        for provider_key, definition in PROVIDER_DEFINITIONS.items():
            raw_values = providers.get(provider_key)
            if not isinstance(raw_values, dict):
                continue

            provider_values: dict[str, str] = {}
            allowed_keys = {field['name'] for field in definition['fields']}
            for field_name, raw_value in raw_values.items():
                if field_name not in allowed_keys:
                    continue
                if not isinstance(raw_value, str):
                    continue
                trimmed = raw_value.strip()
                if trimmed:
                    provider_values[field_name] = trimmed

            if provider_values:
                normalized[provider_key] = provider_values

        return {
            'providers': normalized,
            'updated_at': data.get('updated_at') if isinstance(data, dict) else None,
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        current_providers = current.get('providers', {})
        incoming_providers = payload.get('providers') if isinstance(payload, dict) else {}
        if not isinstance(incoming_providers, dict):
            incoming_providers = {}

        merged: dict[str, dict[str, str]] = {}
        for provider_key, definition in PROVIDER_DEFINITIONS.items():
            existing_values = current_providers.get(provider_key, {})
            incoming_values = incoming_providers.get(provider_key, existing_values)
            if not isinstance(incoming_values, dict):
                incoming_values = existing_values

            provider_values: dict[str, str] = {}
            for field in definition['fields']:
                raw_value = incoming_values.get(field['name'])
                if raw_value is None:
                    continue
                if not isinstance(raw_value, str):
                    raw_value = str(raw_value)
                trimmed = raw_value.strip()
                if trimmed:
                    provider_values[field['name']] = trimmed

            if provider_values:
                merged[provider_key] = provider_values

        data = {
            'providers': merged,
            'updated_at': _utc_now(),
        }

        encrypted = self._fernet().encrypt(json.dumps(data).encode('utf-8'))
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_bytes(encrypted)
        try:
            self.store_path.chmod(0o600)
        except OSError:
            pass

        self.apply_to_env(data)
        return data

    def apply_to_env(self, data: dict[str, Any] | None = None) -> None:
        config = data or self.load()
        providers = config.get('providers', {}) if isinstance(config, dict) else {}

        for key, fallback in ENV_FALLBACKS.items():
            if fallback is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = fallback

        for provider_values in providers.values():
            if not isinstance(provider_values, dict):
                continue
            for field_name, raw_value in provider_values.items():
                if field_name in EDITABLE_ENV_KEYS and isinstance(raw_value, str) and raw_value.strip():
                    os.environ[field_name] = raw_value.strip()

    def get_provider_values(self) -> dict[str, dict[str, Any]]:
        data = self.load()
        stored = data.get('providers', {})
        values: dict[str, dict[str, Any]] = {}

        for provider_key, definition in PROVIDER_DEFINITIONS.items():
            fields: dict[str, str] = {}
            stored_values = stored.get(provider_key, {})
            required = [field['name'] for field in definition['fields'] if field.get('required')]

            for field in definition['fields']:
                field_name = field['name']
                fallback = ENV_FALLBACKS.get(field_name)
                fields[field_name] = stored_values.get(field_name) or fallback or ''

            configured_fields = [name for name, value in fields.items() if value]
            configured = all(fields.get(name) for name in required) if required else bool(configured_fields)
            values[provider_key] = {
                'label': definition['label'],
                'description': definition['description'],
                'fields': definition['fields'],
                'values': fields,
                'configured': configured,
                'source': 'settings' if stored_values else ('env' if configured_fields else 'none'),
            }

        return values

    def has_any_credentials(self) -> bool:
        for provider in self.get_provider_values().values():
            if provider['configured']:
                return True
        return False


@contextmanager
def temporary_env(overrides: dict[str, str | None]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None or not str(value).strip():
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value).strip()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


SETTINGS_STORE = SettingsStore()


def bootstrap_runtime_environment() -> dict[str, str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_STORE.apply_to_env()
    return {
        'flask_secret_key': ensure_persistent_secret('FLASK_SECRET_KEY', 'flask-secret'),
        'agent_token': ensure_persistent_secret('SOCIAL_KANBAN_AGENT_TOKEN', 'agent-token'),
    }
