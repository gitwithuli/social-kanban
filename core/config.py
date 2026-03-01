"""Centralized configuration loader for Social Kanban.

Loads settings from config/settings.yaml with environment variable overrides.
"""

import os
import yaml
from pathlib import Path

_ROOT_DIR = Path(__file__).parent.parent
_SETTINGS_PATH = _ROOT_DIR / "config" / "settings.yaml"

_config = None


def _load_settings():
    """Load settings from YAML file."""
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def get_config():
    """Get the merged configuration (YAML defaults + env overrides). Cached after first call."""
    global _config
    if _config is not None:
        return _config

    settings = _load_settings()
    brand = settings.get("brand", {})

    _config = {
        "brand_name": os.getenv("BRAND_NAME", brand.get("name", "Social Kanban")),
        "handle": os.getenv("BRAND_HANDLE", brand.get("handle", "socialkanban")),
        "tagline": os.getenv("BRAND_TAGLINE", brand.get("tagline", "Plan. Create. Post.")),
        "domain": os.getenv("BRAND_DOMAIN", brand.get("domain", "")),
        "hashtags": os.getenv("BRAND_HASHTAGS", brand.get("hashtags", "#SocialKanban #ContentCreator")),
    }

    return _config


def brand_name():
    return get_config()["brand_name"]


def handle():
    return get_config()["handle"]


def tagline():
    return get_config()["tagline"]


def domain():
    return get_config()["domain"]


def hashtags():
    return get_config()["hashtags"]


def reload():
    """Force reload of configuration (useful for testing)."""
    global _config
    _config = None
    return get_config()
