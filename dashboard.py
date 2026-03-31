#!/usr/bin/env python3
"""Kanban dashboard with smooth drag-and-drop."""
from __future__ import annotations

import os
import json
import secrets
import logging
from functools import wraps
from typing import Any
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from core.models import Quote, Post, PostStatus, init_db, get_session
from core.config import get_config as _get_brand_config
from core.settings_store import (
    PROVIDER_DEFINITIONS,
    SETTINGS_STORE,
    SOCIAL_POST_PROVIDERS,
    bootstrap_runtime_environment,
    temporary_env,
)

RUNTIME_SECRETS = bootstrap_runtime_environment()


def _normalize_app_base_path(value: str | None) -> str:
    if not value:
        return ''
    trimmed = value.strip()
    if not trimmed or trimmed == '/':
        return ''
    if not trimmed.startswith('/'):
        trimmed = '/' + trimmed
    return trimmed.rstrip('/')


APP_BASE_PATH = _normalize_app_base_path(os.getenv('APPLICATION_ROOT'))


class PrefixMiddleware:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path_info = environ.get('PATH_INFO', '')
        if path_info.startswith(self.prefix):
            environ['SCRIPT_NAME'] = self.prefix
            environ['PATH_INFO'] = path_info[len(self.prefix):] or '/'
        return self.app(environ, start_response)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _build_profile():
    cfg = _get_brand_config()
    return {
        'picture_url': os.getenv('PROFILE_PICTURE_URL', ''),
        'name': os.getenv('PROFILE_NAME', cfg['brand_name']),
        'handle': os.getenv('PROFILE_HANDLE', f"@{cfg['handle']}"),
    }

PROFILE_CONFIG = _build_profile()

PLATFORM_LABELS = {
    'twitter': 'X / Twitter',
    'facebook': 'Facebook',
    'instagram': 'Instagram',
    'linkedin': 'LinkedIn',
}


def _platform_label(platform: str | None) -> str:
    if not platform:
        return 'Unknown'
    return PLATFORM_LABELS.get(platform, platform.replace('-', ' ').title())

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', RUNTIME_SECRETS['flask_secret_key'])
if APP_BASE_PATH:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, APP_BASE_PATH)

# Security configuration
app.config.update(
    SESSION_COOKIE_SECURE=os.getenv('FLASK_ENV') == 'production',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=3600,  # 1 hour
    MAX_CONTENT_LENGTH=50 * 1024 * 1024  # 50MB max upload
)


def _full_path(path: str) -> str:
    if not path.startswith('/'):
        path = '/' + path
    return f'{APP_BASE_PATH}{path}' if APP_BASE_PATH else path


def _template_shell() -> str:
    return """
{% if app_base_path %}
<script>
window.SK_APP_BASE_PATH = {{ app_base_path | tojson }};
const __skOriginalFetch = window.fetch.bind(window);
window.fetch = function(resource, init) {
    if (typeof resource === 'string' && resource.startsWith('/')) {
        resource = window.SK_APP_BASE_PATH + resource;
    }
    return __skOriginalFetch(resource, init);
};
</script>
{% else %}
<script>window.SK_APP_BASE_PATH = '';</script>
{% endif %}
"""


def _dashboard_shell() -> str:
    return """
<style>
    .sk-settings-fab {
        position: fixed;
        right: 1.5rem;
        bottom: 1.5rem;
        z-index: 999;
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.75rem 1rem;
        border-radius: 999px;
        border: 1px solid rgba(88, 166, 255, 0.24);
        background: rgba(6, 12, 18, 0.88);
        backdrop-filter: blur(14px);
        color: #e6edf3;
        text-decoration: none;
        font-weight: 600;
        box-shadow: 0 10px 30px rgba(0,0,0,0.25);
    }
    .sk-settings-fab:hover {
        background: rgba(16, 24, 33, 0.96);
        border-color: rgba(88, 166, 255, 0.45);
    }
</style>
""" + _template_shell() + """
<a class="sk-settings-fab" href="{{ app_base_path }}/settings">Settings</a>
"""


def _dashboard_template() -> str:
    return DASHBOARD_TEMPLATE.replace('</body>', _dashboard_shell() + '\n</body>')


def _render_provider_payload() -> dict[str, dict[str, Any]]:
    return SETTINGS_STORE.get_provider_values()


def _needs_onboarding() -> bool:
    return not SETTINGS_STORE.has_any_credentials()


def _is_session_authorized() -> bool:
    password = os.getenv('DASHBOARD_PASSWORD')
    return not password or bool(session.get('authenticated'))


def _is_agent_request() -> bool:
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    token = os.getenv('SOCIAL_KANBAN_AGENT_TOKEN', '')
    return bool(token) and secrets.compare_digest(auth[7:], token)


def login_or_agent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_agent_request() or _is_session_authorized():
            return f(*args, **kwargs)
        return jsonify({'error': 'Unauthorized'}), 401
    return decorated


def _parse_scheduled_at(value: Any) -> datetime | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError('scheduled_at must be an ISO-8601 string')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('scheduled_at must be a valid ISO-8601 timestamp') from exc


def _normalize_platforms(value: Any) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError('platform is required')
        if normalized == 'all':
            return list(SOCIAL_POST_PROVIDERS)
        candidates = [normalized]
    elif isinstance(value, list):
        candidates = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError('platform must be a string or list of strings')
            trimmed = entry.strip().lower()
            if trimmed:
                candidates.append(trimmed)
    else:
        raise ValueError('platform must be a string or list of strings')

    if not candidates:
        raise ValueError('platform is required')

    invalid = [platform for platform in candidates if platform not in SOCIAL_POST_PROVIDERS]
    if invalid:
        raise ValueError(f'Unsupported platforms: {", ".join(sorted(set(invalid)))}')

    return list(dict.fromkeys(candidates))


def _provider_test_overrides(provider: str, values: dict[str, Any]) -> dict[str, str | None]:
    definition = PROVIDER_DEFINITIONS[provider]
    overrides: dict[str, str | None] = {}
    for field in definition['fields']:
        raw_value = values.get(field['name']) if isinstance(values, dict) else None
        if raw_value is None:
            raw_value = os.getenv(field['name'])
        overrides[field['name']] = None if raw_value is None else str(raw_value)
    return overrides


def _test_provider_connection(provider: str, values: dict[str, Any]) -> dict[str, Any]:
    overrides = _provider_test_overrides(provider, values)
    with temporary_env(overrides):
        if provider == 'anthropic':
            api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
            if not api_key:
                return {'configured': False, 'error': 'Anthropic API key not set'}
            import requests
            response = requests.get(
                'https://api.anthropic.com/v1/models',
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                },
                timeout=30,
            )
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code >= 400:
                return {'configured': False, 'error': data.get('error', {}).get('message') or response.text or 'Anthropic authentication failed'}
            return {'configured': True, 'status': 'ok'}

        if provider == 'groq':
            api_key = os.getenv('GROQ_API_KEY', '').strip()
            if not api_key:
                return {'configured': False, 'error': 'Groq API key not set'}
            import requests
            response = requests.get(
                'https://api.groq.com/openai/v1/models',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30,
            )
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code >= 400:
                return {'configured': False, 'error': data.get('error', {}).get('message') or response.text or 'Groq authentication failed'}
            return {'configured': True, 'status': 'ok'}

        if provider == 'twitter':
            from integrations.twitter_client import TwitterClient
            return TwitterClient(dry_run=False).verify_credentials()

        if provider == 'facebook':
            from integrations.facebook_client import FacebookClient
            return FacebookClient().verify_credentials()

        if provider == 'instagram':
            from integrations.instagram_client import InstagramClient
            return InstagramClient().verify_credentials()

        if provider == 'cloudinary':
            from integrations.cloudinary_client import CloudinaryClient
            return CloudinaryClient().verify_credentials()

        if provider == 'linkedin':
            from integrations.linkedin_client import LinkedInClient
            return LinkedInClient().verify_credentials()

    return {'configured': False, 'error': 'Unknown provider'}

def login_required(f):
    """Require authentication if DASHBOARD_PASSWORD is set."""
    @wraps(f)
    def decorated(*args, **kwargs):
        password = os.getenv('DASHBOARD_PASSWORD')
        if password and not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page — only active when DASHBOARD_PASSWORD is set."""
    password = os.getenv('DASHBOARD_PASSWORD')
    if not password:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        if request.form.get('password') == password:
            session['authenticated'] = True
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template_string(LOGIN_TEMPLATE, error='Invalid password')

    return render_template_string(LOGIN_TEMPLATE, error=None)


@app.route('/logout')
def logout():
    """Logout and clear session."""
    session.clear()
    return redirect(url_for('login'))


LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login — Social Kanban</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Outfit', sans-serif;
            background: #08090a;
            color: #e6edf3;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-card {
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 16px;
            padding: 3rem;
            width: 100%;
            max-width: 380px;
        }
        .login-card h1 {
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
        }
        .login-card p {
            color: #8b949e;
            margin-bottom: 2rem;
            font-size: 0.9rem;
        }
        .login-card input {
            width: 100%;
            padding: 0.75rem 1rem;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #e6edf3;
            font-size: 1rem;
            font-family: inherit;
            margin-bottom: 1rem;
        }
        .login-card input:focus {
            outline: none;
            border-color: #58a6ff;
        }
        .login-card button {
            width: 100%;
            padding: 0.75rem;
            background: linear-gradient(135deg, #00d4ff, #58a6ff);
            border: none;
            border-radius: 8px;
            color: #000;
            font-weight: 600;
            font-size: 1rem;
            cursor: pointer;
            font-family: inherit;
        }
        .login-card button:hover {
            opacity: 0.9;
        }
        .error {
            color: #f87171;
            font-size: 0.85rem;
            margin-bottom: 1rem;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>Social Kanban</h1>
        <p>Enter your password to continue.</p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="Password" autofocus required>
            <button type="submit">Sign in</button>
        </form>
    </div>
</body>
</html>
"""

ONBOARDING_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connect Your Socials — Social Kanban</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: 'Outfit', sans-serif;
            background:
                radial-gradient(circle at top, rgba(88, 166, 255, 0.18), transparent 30%),
                linear-gradient(180deg, #07090d 0%, #0b0f14 100%);
            color: #e6edf3;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 2rem;
        }
        .shell {
            width: min(960px, 100%);
            display: grid;
            grid-template-columns: 1.2fr 0.8fr;
            gap: 1.5rem;
        }
        .card {
            background: rgba(9, 14, 20, 0.88);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 24px;
            padding: 2rem;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
        }
        h1 {
            margin: 0 0 0.75rem;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.05;
        }
        p.lead {
            margin: 0 0 1.25rem;
            color: #9fb0c0;
            font-size: 1.05rem;
            line-height: 1.6;
        }
        .cta-row {
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            margin-top: 1.25rem;
        }
        .btn {
            border: none;
            border-radius: 999px;
            padding: 0.85rem 1.25rem;
            font: inherit;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
        }
        .btn-primary {
            background: linear-gradient(135deg, #58a6ff, #7ee787);
            color: #07111c;
        }
        .btn-secondary {
            background: rgba(255, 255, 255, 0.03);
            color: #dbe8f5;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .providers {
            display: grid;
            gap: 0.75rem;
        }
        .provider {
            padding: 0.9rem 1rem;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.06);
        }
        .provider strong {
            display: block;
            margin-bottom: 0.25rem;
        }
        .provider span {
            color: #8fa0b2;
            font-size: 0.92rem;
            line-height: 1.5;
        }
        .eyebrow {
            display: inline-block;
            margin-bottom: 0.8rem;
            padding: 0.35rem 0.6rem;
            border-radius: 999px;
            background: rgba(88, 166, 255, 0.12);
            color: #9dcbff;
            letter-spacing: 0.12em;
            font-size: 0.72rem;
            text-transform: uppercase;
        }
        @media (max-width: 780px) {
            .shell { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <section class="card">
            <span class="eyebrow">First Run</span>
            <h1>Connect your socials before you start filling the board.</h1>
            <p class="lead">
                Social Kanban stays lightweight until you add the accounts and AI keys you actually want.
                No `.env` edits, no manual Docker steps, and no fake starter data.
            </p>
            <div class="cta-row">
                <a class="btn btn-primary" href="{{ app_base_path }}/settings">Open Settings</a>
                <a class="btn btn-secondary" href="{{ app_base_path }}/settings#agent-hook">Configure Agent Hook</a>
            </div>
        </section>
        <aside class="card">
            <span class="eyebrow">Supported</span>
            <div class="providers">
                {% for provider in providers.values() %}
                <div class="provider">
                    <strong>{{ provider.label }}</strong>
                    <span>{{ provider.description }}</span>
                </div>
                {% endfor %}
            </div>
        </aside>
    </div>
</body>
</html>
"""

SETTINGS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Settings — Social Kanban</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: 'Outfit', sans-serif;
            background: #07090d;
            color: #e6edf3;
        }
        .page {
            width: min(1180px, 100%);
            margin: 0 auto;
            padding: 2rem 1.25rem 4rem;
        }
        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        .topbar h1 {
            margin: 0 0 0.5rem;
            font-size: clamp(2rem, 4vw, 2.8rem);
        }
        .topbar p {
            margin: 0;
            color: #8ea0b3;
            line-height: 1.6;
        }
        .nav {
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
        }
        .nav a, .nav button {
            text-decoration: none;
            border: 1px solid rgba(255,255,255,0.12);
            background: rgba(255,255,255,0.03);
            color: #dbe8f5;
            padding: 0.75rem 1rem;
            border-radius: 999px;
            font: inherit;
            font-weight: 600;
            cursor: pointer;
        }
        .notice {
            margin-bottom: 1.5rem;
            padding: 1rem 1.1rem;
            border-radius: 16px;
            border: 1px solid rgba(126, 231, 135, 0.22);
            background: rgba(126, 231, 135, 0.08);
            color: #c7f9cf;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1rem;
        }
        .provider-card, .meta-card {
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 22px;
            padding: 1.25rem;
        }
        .provider-card h2, .meta-card h2 {
            margin: 0 0 0.4rem;
            font-size: 1.1rem;
        }
        .provider-card p, .meta-card p {
            margin: 0 0 0.9rem;
            color: #8b949e;
            line-height: 1.5;
        }
        .provider-status {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            margin-bottom: 1rem;
            padding: 0.35rem 0.7rem;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 600;
            background: rgba(88, 166, 255, 0.12);
            color: #9dcbff;
        }
        .provider-status.ready {
            background: rgba(126, 231, 135, 0.12);
            color: #8bf1a0;
        }
        .field {
            display: grid;
            gap: 0.35rem;
            margin-bottom: 0.8rem;
        }
        .field span {
            font-size: 0.85rem;
            color: #b7c4d1;
        }
        .field input {
            width: 100%;
            padding: 0.8rem 0.95rem;
            border-radius: 12px;
            border: 1px solid #30363d;
            background: #11161d;
            color: #e6edf3;
            font: inherit;
        }
        .provider-actions {
            display: flex;
            gap: 0.65rem;
            flex-wrap: wrap;
            margin-top: 1rem;
        }
        .provider-actions button,
        .save-bar button {
            border: none;
            border-radius: 12px;
            padding: 0.8rem 1rem;
            font: inherit;
            font-weight: 600;
            cursor: pointer;
        }
        .provider-actions .primary,
        .save-bar .primary {
            background: linear-gradient(135deg, #58a6ff, #7ee787);
            color: #07111c;
        }
        .provider-actions .secondary,
        .save-bar .secondary {
            background: rgba(255,255,255,0.04);
            color: #dbe8f5;
            border: 1px solid rgba(255,255,255,0.12);
        }
        .provider-result {
            min-height: 1.25rem;
            margin-top: 0.9rem;
            font-size: 0.85rem;
            color: #8b949e;
        }
        .provider-result.ok {
            color: #8bf1a0;
        }
        .provider-result.error {
            color: #ff9b9b;
        }
        .save-bar {
            position: sticky;
            bottom: 1rem;
            margin-top: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            padding: 1rem 1.1rem;
            border-radius: 18px;
            background: rgba(8, 12, 18, 0.92);
            backdrop-filter: blur(14px);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .save-bar .message {
            color: #9fb0c0;
        }
        code.token {
            display: block;
            margin-top: 0.7rem;
            padding: 0.75rem 0.9rem;
            border-radius: 12px;
            background: #11161d;
            border: 1px solid #30363d;
            color: #8bf1a0;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-all;
        }
        @media (max-width: 700px) {
            .topbar, .save-bar { flex-direction: column; align-items: stretch; }
        }
    </style>
</head>
<body>
    <div class="page">
        <div class="topbar">
            <div>
                <h1>Provider Settings</h1>
                <p>Store your platform keys once, test them in place, and let the board run without manual `.env` editing.</p>
            </div>
            <div class="nav">
                <a href="{{ app_base_path }}/">Back to Board</a>
            </div>
        </div>

        {% if onboarding %}
        <div class="notice">
            This is the first-run setup. Add the providers you care about now, save, then go back to the board.
        </div>
        {% endif %}

        <div class="grid">
            {% for provider_key, provider in providers.items() %}
            <section class="provider-card" data-provider="{{ provider_key }}">
                <h2>{{ provider.label }}</h2>
                <p>{{ provider.description }}</p>
                <div class="provider-status {{ 'ready' if provider.configured else '' }}" id="status-{{ provider_key }}">
                    {{ 'Configured' if provider.configured else 'Not configured' }}
                    {% if provider.source == 'env' %} via env{% elif provider.source == 'settings' %} via UI{% endif %}
                </div>
                {% for field in provider.fields %}
                <label class="field">
                    <span>{{ field.label }}{% if field.required %} *{% endif %}</span>
                    <input
                        type="{{ 'password' if field.secret else 'text' }}"
                        data-field="{{ field.name }}"
                        placeholder="{{ field.placeholder }}"
                        value="{{ provider.values[field.name] }}"
                    >
                </label>
                {% endfor %}
                <div class="provider-actions">
                    <button class="secondary" type="button" onclick="testProvider('{{ provider_key }}')">Test Connection</button>
                </div>
                <div class="provider-result" id="result-{{ provider_key }}"></div>
            </section>
            {% endfor %}

            <section class="meta-card" id="agent-hook">
                <h2>Agent Hook</h2>
                <p>
                    Ekuri can push drafts directly into the kanban pipeline with
                    <code>POST {{ app_base_path }}/api/posts</code>.
                    Use the bearer token below from local or VPS automation.
                </p>
                <code class="token">{{ agent_token }}</code>
                <p style="margin-top: 0.9rem;">
                    Body shape:
                    <code>{"content":"Post body","platform":["twitter","linkedin"],"scheduled_at":"2026-04-01T15:00:00Z"}</code>
                </p>
            </section>
        </div>

        <div class="save-bar">
            <div class="message" id="save-message">Changes are stored in an encrypted local settings file inside <code>data/</code>.</div>
            <div class="nav">
                <button class="secondary" type="button" onclick="window.location.href='{{ app_base_path }}/'">Open Board</button>
                <button class="primary" type="button" onclick="saveSettings()">Save Settings</button>
            </div>
        </div>
    </div>

    <script>
        const APP_BASE_PATH = {{ app_base_path | tojson }};

        function providerCards() {
            return Array.from(document.querySelectorAll('[data-provider]'));
        }

        function collectProvider(providerKey) {
            const card = document.querySelector(`[data-provider="${providerKey}"]`);
            const payload = {};
            for (const input of card.querySelectorAll('input[data-field]')) {
                payload[input.dataset.field] = input.value;
            }
            return payload;
        }

        function collectAllProviders() {
            const providers = {};
            for (const card of providerCards()) {
                providers[card.dataset.provider] = collectProvider(card.dataset.provider);
            }
            return providers;
        }

        function setProviderResult(providerKey, kind, message) {
            const el = document.getElementById(`result-${providerKey}`);
            el.className = `provider-result ${kind}`;
            el.textContent = message;
        }

        async function testProvider(providerKey) {
            setProviderResult(providerKey, '', 'Testing connection…');
            try {
                const response = await fetch(`${APP_BASE_PATH}/api/settings/test/${providerKey}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ values: collectProvider(providerKey) }),
                });
                const data = await response.json();
                if (!response.ok || data.configured === false) {
                    setProviderResult(providerKey, 'error', data.error || data.message || 'Connection failed');
                    return;
                }
                const summary = data.username || data.page_name || data.name || data.handle || data.cloud_name || data.email || 'Connection ok';
                setProviderResult(providerKey, 'ok', `Connected: ${summary}`);
            } catch (error) {
                setProviderResult(providerKey, 'error', error.message || 'Connection failed');
            }
        }

        async function saveSettings() {
            const message = document.getElementById('save-message');
            message.textContent = 'Saving settings…';
            try {
                const response = await fetch(`${APP_BASE_PATH}/api/settings`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ providers: collectAllProviders() }),
                });
                const data = await response.json();
                if (!response.ok || !data.success) {
                    message.textContent = data.error || 'Failed to save settings';
                    return;
                }
                message.textContent = 'Settings saved. The board now uses your updated credentials.';
                for (const [providerKey, provider] of Object.entries(data.providers || {})) {
                    const badge = document.getElementById(`status-${providerKey}`);
                    if (!badge) continue;
                    const ready = provider.configured;
                    badge.textContent = ready
                        ? `Configured${provider.source === 'settings' ? ' via UI' : provider.source === 'env' ? ' via env' : ''}`
                        : 'Not configured';
                    badge.className = `provider-status ${ready ? 'ready' : ''}`;
                }
            } catch (error) {
                message.textContent = error.message || 'Failed to save settings';
            }
        }
    </script>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Social Kanban</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #08090a;
            --bg-card: #0d1117;
            --bg-elevated: #161b22;
            --border: #21262d;
            --border-bright: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --accent-cyan: #00d4ff;
            --accent-purple: #a78bfa;
            --accent-blue: #58a6ff;
            --accent-green: #4ade80;
            --accent-yellow: #fbbf24;
            --accent-red: #f87171;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-deep);
            min-height: 100vh;
            color: var(--text-primary);
            overflow: hidden;
        }

        .header {
            background: linear-gradient(90deg, rgba(0,212,255,0.06), rgba(167,139,250,0.06));
            border-bottom: 1px solid var(--border);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            backdrop-filter: blur(12px);
        }

        .logo {
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.25rem;
            font-weight: 600;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .stats-bar { display: flex; gap: 2rem; font-size: 0.8rem; }
        .stat { display: flex; align-items: center; gap: 0.5rem; }
        .stat-num {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            color: var(--accent-cyan);
        }
        .stat-label { color: var(--text-muted); text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; }

        .kanban {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 1rem;
            padding: 1.25rem;
            height: calc(100vh - 65px);
        }

        .column {
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            transition: border-color 0.3s cubic-bezier(0.4, 0, 0.2, 1),
                        box-shadow 0.3s cubic-bezier(0.4, 0, 0.2, 1),
                        transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .column.drag-over {
            border-color: var(--accent-blue);
            box-shadow: 0 0 0 1px var(--accent-blue),
                        inset 0 0 30px rgba(88, 166, 255, 0.08),
                        0 8px 32px rgba(88, 166, 255, 0.12);
            transform: scale(1.01);
        }

        .column-header {
            padding: 1rem 1rem 0.75rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .column-title {
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
        }
        .column-count {
            font-family: 'JetBrains Mono', monospace;
            background: var(--border);
            color: var(--text-muted);
            padding: 0.2rem 0.6rem;
            border-radius: 6px;
            font-size: 0.7rem;
            font-weight: 500;
        }

        .column-body {
            flex: 1;
            overflow-y: auto;
            padding: 0.5rem;
            min-height: 200px;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 0.875rem;
            margin-bottom: 0.5rem;
            cursor: grab;
            position: relative;
            user-select: none;
            -webkit-user-select: none;
            touch-action: none;
            will-change: transform, opacity, box-shadow;
            transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.2s cubic-bezier(0.4, 0, 0.2, 1),
                        border-color 0.2s ease;
        }

        .card:hover {
            border-color: var(--accent-blue);
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(88, 166, 255, 0.2);
        }

        .card:active { cursor: grabbing; }

        .card.is-dragging {
            opacity: 0.4;
            transform: scale(0.97);
            border-style: dashed;
            border-color: var(--accent-cyan);
            background: rgba(0, 212, 255, 0.03);
            transition: opacity 0.15s ease, transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
        }

        .drag-ghost {
            position: fixed;
            pointer-events: none;
            z-index: 10000;
            will-change: transform, opacity;
            border-radius: 10px;
            background: var(--bg-card);
            border: 2px solid var(--accent-cyan);
            box-shadow: 0 25px 60px rgba(0, 0, 0, 0.5),
                        0 0 0 1px rgba(0, 212, 255, 0.3),
                        0 0 40px rgba(0, 212, 255, 0.15);
            opacity: 0;
            backface-visibility: hidden;
            -webkit-backface-visibility: hidden;
            transform-style: preserve-3d;
        }

        .drag-ghost.visible {
            opacity: 1;
            transition: opacity 0.12s ease-out;
        }

        .drag-ghost.dropping {
            opacity: 0;
            transition: transform 0.28s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.22s ease-out;
        }

        .card-content {
            font-size: 0.8rem;
            line-height: 1.55;
            margin-bottom: 0.625rem;
            color: var(--text-primary);
        }

        .card-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 0.375rem;
            align-items: center;
        }

        .tag {
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            font-size: 0.65rem;
            font-weight: 500;
            font-family: 'JetBrains Mono', monospace;
        }

        .tag-topic { background: rgba(167, 139, 250, 0.15); color: var(--accent-purple); }
        .tag-source {
            padding: 0.2rem 0.6rem;
            max-width: 140px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .tag-score { background: rgba(88, 166, 255, 0.15); color: var(--accent-blue); }

        .card.approved { border-left: 3px solid var(--accent-green); }
        .card.pending { border-left: 3px solid var(--accent-yellow); }
        .card.posted { border-left: 3px solid var(--accent-blue); opacity: 0.7; }

        .post-meta { justify-content: space-between; font-size: 0.7rem; color: var(--text-secondary); }

        .status-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 0.35rem;
        }
        .status-dot.approved { background: var(--accent-green); box-shadow: 0 0 8px var(--accent-green); }
        .status-dot.pending { background: var(--accent-yellow); box-shadow: 0 0 8px var(--accent-yellow); }
        .status-dot.posted { background: var(--accent-blue); box-shadow: 0 0 8px var(--accent-blue); }
        .status-dot.quote { background: var(--accent-purple); box-shadow: 0 0 8px var(--accent-purple); }

        .char-count { font-size: 0.65rem; font-family: 'JetBrains Mono', monospace; }
        .char-ok { color: var(--accent-green); }
        .char-warn { color: var(--accent-yellow); }
        .char-over { color: var(--accent-red); }

        .col-quotes .column-header { border-bottom: 2px solid var(--text-muted); }
        .col-approved .column-header { border-bottom: 2px solid var(--accent-green); }
        .col-pending .column-header { border-bottom: 2px solid var(--accent-yellow); }
        .col-posted .column-header { border-bottom: 2px solid var(--accent-blue); }

        .btn-shuffle {
            background: transparent;
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-secondary);
            padding: 0.3rem 0.6rem;
            font-size: 0.7rem;
            font-family: 'Outfit', sans-serif;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.35rem;
            transition: all 0.2s ease;
        }

        .btn-shuffle:hover {
            border-color: var(--accent-cyan);
            color: var(--accent-cyan);
            background: rgba(0, 212, 255, 0.08);
        }

        .btn-shuffle svg {
            width: 12px;
            height: 12px;
        }

        .btn-shuffle.shuffling svg {
            animation: shuffleSpin 0.5s ease;
        }

        @keyframes shuffleSpin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .column-header-left {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .empty-state {
            text-align: center;
            padding: 2rem 1rem;
            color: var(--text-muted);
            font-size: 0.8rem;
            border: 2px dashed var(--border);
            border-radius: 10px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .empty-state.drag-over {
            border-color: var(--accent-blue);
            background: rgba(88, 166, 255, 0.08);
            color: var(--accent-blue);
        }

        .drop-placeholder {
            height: 4px;
            border-radius: 2px;
            background: linear-gradient(90deg, var(--accent-cyan), var(--accent-purple));
            margin: 0.25rem 0;
            opacity: 0;
            transform: scaleX(0.3);
            transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
            box-shadow: 0 0 12px var(--accent-cyan);
        }

        .drop-placeholder.visible {
            opacity: 1;
            transform: scaleX(1);
        }

        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            background: linear-gradient(135deg, #238636, #2ea043);
            color: white;
            padding: 0.875rem 1.5rem;
            border-radius: 10px;
            font-size: 0.875rem;
            font-weight: 500;
            opacity: 0;
            transform: translateY(20px) scale(0.95);
            transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
            z-index: 10001;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        }
        .toast.show { opacity: 1; transform: translateY(0) scale(1); }
        .toast.error { background: linear-gradient(135deg, #da3633, #f85149); }

        .column-body::-webkit-scrollbar { width: 4px; }
        .column-body::-webkit-scrollbar-track { background: transparent; }
        .column-body::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 2px; }

        /* Modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(8px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 9999;
            opacity: 0;
            transition: opacity 0.2s ease;
        }

        .modal-overlay.show {
            display: flex;
            opacity: 1;
        }

        .modal {
            background: var(--bg-deep);
            border: 1px solid var(--border-bright);
            border-radius: 16px;
            width: 100%;
            max-width: 560px;
            max-height: 90vh;
            overflow-y: auto;
            transform: scale(0.95) translateY(10px);
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .modal-overlay.show .modal {
            transform: scale(1) translateY(0);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 1.25rem;
            border-bottom: 1px solid var(--border);
        }

        .modal-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.5rem;
            border-radius: 50%;
            transition: all 0.2s;
            line-height: 1;
            position: relative;
            z-index: 10;
        }

        .modal-close:hover {
            background: rgba(255,255,255,0.08);
            color: var(--text-primary);
        }

        .x-post { padding: 1.25rem; }

        .x-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }

        .x-avatar {
            width: 44px;
            height: 44px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.1rem;
            color: white;
            font-family: 'JetBrains Mono', monospace;
            overflow: hidden;
        }
        .x-avatar img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .x-user-info { flex: 1; }
        .x-name { font-weight: 600; color: var(--text-primary); font-size: 0.95rem; }
        .x-handle { color: var(--text-muted); font-size: 0.85rem; }

        .x-content {
            color: var(--text-primary);
            font-size: 1.05rem;
            line-height: 1.5;
            white-space: pre-wrap;
            margin-bottom: 1rem;
        }

        .x-hashtag { color: #1d9bf0; }

        .x-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 0;
            border-top: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        .x-char-count { font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; }

        .modal-draft-actions {
            display: flex;
            justify-content: flex-start;
            gap: 0.75rem;
            flex-wrap: wrap;
            padding: 0.5rem 0;
            border-top: 1px solid var(--border);
        }

        .btn-draft-edit,
        .btn-draft-delete {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.45rem;
            padding: 0.55rem 0.95rem;
            border-radius: 8px;
            border: 1px solid var(--border-bright);
            font-family: 'Outfit', sans-serif;
            font-size: 0.84rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            background: transparent;
        }

        .btn-draft-edit {
            color: var(--text-primary);
        }

        .btn-draft-edit:hover {
            background: rgba(88, 166, 255, 0.1);
            border-color: rgba(88, 166, 255, 0.35);
            color: var(--accent-cyan);
        }

        .btn-draft-delete {
            color: #f87171;
            border-color: rgba(248, 113, 113, 0.28);
        }

        .btn-draft-delete:hover {
            background: rgba(248, 113, 113, 0.1);
            border-color: rgba(248, 113, 113, 0.42);
            color: #fca5a5;
        }

        .modal-status {
            padding: 0.75rem 1.25rem;
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-elevated);
        }

        .status-label {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.8rem;
            font-weight: 500;
        }

        .status-label.pending { color: var(--accent-yellow); }
        .status-label.approved { color: var(--accent-green); }
        .status-label.posted { color: var(--accent-blue); }
        .status-label.quote { color: var(--accent-purple); }

        body.is-dragging { cursor: grabbing !important; }
        body.is-dragging * { cursor: grabbing !important; }

        .btn-post-x {
            display: inline-block;
            background: #000;
            color: #fff;
            border: 1px solid #333;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn-post-x:hover {
            background: #1d9bf0;
            border-color: #1d9bf0;
            color: #fff;
        }

        /* Image Generator */
        .btn-generate-img {
            display: inline-block;
            background: linear-gradient(135deg, #E1306C, #F77737);
            color: #fff;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            margin-left: 0.5rem;
        }
        .btn-generate-img:hover {
            transform: scale(1.05);
            box-shadow: 0 4px 15px rgba(225, 48, 108, 0.4);
        }

        .btn-post-instagram {
            display: inline-block;
            background: linear-gradient(135deg, #833AB4, #E1306C, #F77737);
            color: #fff;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn-post-instagram:hover {
            transform: scale(1.05);
            box-shadow: 0 4px 15px rgba(225, 48, 108, 0.4);
        }
        .btn-post-instagram:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        .btn-post-facebook {
            display: inline-block;
            background: #1877F2;
            color: #fff;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn-post-facebook:hover {
            background: #166FE5;
            transform: scale(1.05);
            box-shadow: 0 4px 15px rgba(24, 119, 242, 0.4);
        }
        .btn-post-facebook:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        .btn-post-linkedin {
            display: inline-block;
            background: #0A66C2;
            color: #fff;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn-post-linkedin:hover {
            background: #08549f;
            transform: scale(1.05);
            box-shadow: 0 4px 15px rgba(10, 102, 194, 0.35);
        }
        .btn-post-linkedin:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }

        .image-generator-modal {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.92);
            backdrop-filter: blur(10px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10001;
            opacity: 0;
            transition: opacity 0.25s ease;
        }
        .image-generator-modal.show {
            display: flex;
            opacity: 1;
        }

        .img-gen-panel {
            background: var(--bg-elevated);
            border: 1px solid var(--border-bright);
            border-radius: 16px;
            width: 100%;
            max-width: 900px;
            max-height: 95vh;
            overflow-y: auto;
            padding: 1.5rem;
        }

        .img-gen-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }

        .img-gen-title {
            font-size: 1.2rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .img-gen-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.25rem;
            line-height: 1;
            border-radius: 6px;
            transition: all 0.2s;
        }
        .img-gen-close:hover {
            background: rgba(255,255,255,0.08);
            color: var(--text-primary);
        }

        .img-gen-content {
            display: grid;
            grid-template-columns: 1fr 280px;
            gap: 1.5rem;
        }

        .img-preview-container {
            display: flex;
            align-items: center;
            justify-content: center;
            background: #1a1a1a;
            border-radius: 12px;
            padding: 1rem;
            min-height: 400px;
        }

        #tweetCanvas {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }

        .img-gen-controls {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .control-group {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .control-label {
            font-size: 0.75rem;
            font-weight: 500;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .theme-options {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.5rem;
        }

        .theme-btn {
            padding: 0.6rem;
            border-radius: 8px;
            border: 2px solid var(--border);
            background: var(--bg-card);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            font-size: 0.8rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .theme-btn:hover {
            border-color: var(--accent-cyan);
        }
        .theme-btn.active {
            border-color: var(--accent-cyan);
            background: rgba(0, 212, 255, 0.1);
        }

        .theme-btn.brand { background: #1a3a2f; color: #c9a227; }
        .theme-btn.minimal { background: #fafafa; color: #1a1a1a; }
        .theme-btn.bold { background: linear-gradient(135deg, #1a3a2f, #0d1f17); color: #c9a227; }
        .theme-btn.dark { background: #0d1117; color: #e6edf3; }
        .theme-btn.edge { background: #FAF7F2; color: #C45A3B; border-color: #8B9A7D; }
        .theme-btn.stoic { background: #0f0f0f; color: #C45A3B; font-style: italic; }

        .dimension-options {
            display: flex;
            gap: 0.5rem;
        }

        .dim-btn {
            flex: 1;
            padding: 0.5rem;
            border-radius: 8px;
            border: 2px solid var(--border);
            background: var(--bg-card);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.2s;
        }
        .dim-btn:hover { border-color: var(--accent-cyan); }
        .dim-btn.active {
            border-color: var(--accent-cyan);
            background: rgba(0, 212, 255, 0.1);
        }

        .img-gen-actions {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            margin-top: auto;
            padding-top: 1rem;
        }

        .btn-download {
            width: 100%;
            padding: 0.875rem;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            border: none;
            border-radius: 10px;
            color: white;
            font-family: 'Outfit', sans-serif;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
        }
        .btn-download:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 20px rgba(0, 212, 255, 0.3);
        }

        .share-buttons {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 0.5rem;
        }

        .btn-share {
            padding: 0.6rem;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--bg-card);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            font-size: 0.8rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.4rem;
        }
        .btn-share:hover {
            border-color: var(--text-secondary);
            background: var(--bg-elevated);
        }
        .btn-share.instagram { border-color: #E1306C; color: #E1306C; }
        .btn-share.instagram:hover { background: rgba(225, 48, 108, 0.1); }
        .btn-share.linkedin { border-color: #0A66C2; color: #0A66C2; }
        .btn-share.linkedin:hover { background: rgba(10, 102, 194, 0.1); }
        .btn-share.facebook { border-color: #1877F2; color: #1877F2; }
        .btn-share.facebook:hover { background: rgba(24, 119, 242, 0.1); }

        /* Create Button */
        .btn-create {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            border: none;
            border-radius: 8px;
            color: white;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 2px 8px rgba(0, 212, 255, 0.25);
        }

        .btn-create:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 16px rgba(0, 212, 255, 0.35);
        }

        .btn-create:active {
            transform: translateY(0);
        }

        .btn-create svg {
            width: 16px;
            height: 16px;
        }

        .btn-stoic {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: linear-gradient(135deg, #C45A3B, #8B3A2A);
            border: none;
            border-radius: 8px;
            color: white;
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 2px 8px rgba(196, 90, 59, 0.25);
        }

        .btn-stoic:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 16px rgba(196, 90, 59, 0.35);
        }

        .btn-stoic:active {
            transform: translateY(0);
        }

        .btn-stoic svg {
            width: 16px;
            height: 16px;
        }

        /* Stoic Modal */
        .stoic-modal {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.9);
            backdrop-filter: blur(12px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            opacity: 0;
            transition: opacity 0.25s ease;
        }

        .stoic-modal.show {
            display: flex;
            opacity: 1;
        }

        .stoic-panel {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 100%;
            max-width: 520px;
            max-height: 90vh;
            overflow: hidden;
            transform: scale(0.95) translateY(10px);
            transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .stoic-modal.show .stoic-panel {
            transform: scale(1) translateY(0);
        }

        .stoic-header {
            padding: 1.25rem 1.5rem;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .stoic-title {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .stoic-title-icon {
            color: #C45A3B;
        }

        .stoic-close {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.25rem;
            line-height: 1;
            transition: color 0.2s;
        }

        .stoic-close:hover {
            color: var(--text-primary);
        }

        .stoic-body {
            padding: 1.5rem;
            overflow-y: auto;
            max-height: calc(90vh - 140px);
        }

        .stoic-date-info {
            text-align: center;
            margin-bottom: 1.25rem;
        }

        .stoic-date {
            font-size: 0.85rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .stoic-entry-title {
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-top: 0.5rem;
        }

        .stoic-author {
            font-size: 0.9rem;
            color: #C45A3B;
            margin-top: 0.25rem;
        }

        .stoic-preview {
            margin: 1.25rem 0;
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--border);
        }

        .stoic-preview img {
            width: 100%;
            height: auto;
            display: block;
        }

        .stoic-tweet-preview {
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1rem;
            margin-top: 1rem;
        }

        .stoic-tweet-label {
            font-size: 0.75rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 0.5rem;
        }

        .stoic-tweet-text {
            font-size: 0.9rem;
            color: var(--text-primary);
            line-height: 1.5;
        }

        .stoic-actions {
            display: flex;
            gap: 0.75rem;
            padding: 1rem 1.5rem;
            border-top: 1px solid var(--border);
            background: var(--bg-elevated);
        }

        .stoic-btn {
            flex: 1;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            font-family: 'Outfit', sans-serif;
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
        }

        .stoic-btn-generate {
            background: linear-gradient(135deg, #C45A3B, #8B3A2A);
            border: none;
            color: white;
        }

        .stoic-btn-generate:hover:not(:disabled) {
            box-shadow: 0 4px 16px rgba(196, 90, 59, 0.35);
        }

        .stoic-btn-generate:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .stoic-btn-queue {
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            border: none;
            color: white;
        }

        .stoic-btn-queue:hover {
            box-shadow: 0 4px 16px rgba(0, 212, 255, 0.35);
        }

        .stoic-btn-cancel {
            background: var(--bg-card);
            border: 1px solid var(--border);
            color: var(--text-secondary);
        }

        .stoic-btn-cancel:hover {
            border-color: var(--border-bright);
            color: var(--text-primary);
        }

        .stoic-loading {
            text-align: center;
            padding: 2rem;
        }

        .stoic-spinner {
            width: 40px;
            height: 40px;
            border: 3px solid var(--border);
            border-top-color: #C45A3B;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 1rem;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .stoic-loading-text {
            color: var(--text-secondary);
            font-size: 0.9rem;
        }

        .stoic-error {
            background: rgba(248, 113, 113, 0.1);
            border: 1px solid rgba(248, 113, 113, 0.3);
            color: #f87171;
            padding: 1rem;
            border-radius: 8px;
            text-align: center;
            margin: 1rem 0;
        }

        .attachment-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            margin-top: 0.7rem;
            padding: 0.28rem 0.55rem;
            border-radius: 999px;
            background: rgba(0, 212, 255, 0.08);
            border: 1px solid rgba(0, 212, 255, 0.18);
            color: var(--accent-cyan);
            font-size: 0.72rem;
            font-weight: 600;
        }

        /* Draft Modal */
        .draft-modal {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.9);
            backdrop-filter: blur(12px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            opacity: 0;
            transition: opacity 0.25s ease;
        }

        .draft-modal.show {
            display: flex;
            opacity: 1;
        }

        .draft-panel {
            width: min(720px, calc(100vw - 2rem));
            max-height: calc(100vh - 2rem);
            overflow-y: auto;
            background: var(--bg-elevated);
            border: 1px solid var(--border-bright);
            border-radius: 18px;
            padding: 1.6rem;
            transform: scale(0.96) translateY(10px);
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
        }

        .draft-modal.show .draft-panel {
            transform: scale(1) translateY(0);
        }

        .draft-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            margin-bottom: 1.25rem;
        }

        .draft-title-wrap {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }

        .draft-title {
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--text-primary);
        }

        .draft-subtitle {
            font-size: 0.9rem;
            color: var(--text-secondary);
        }

        .draft-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.2rem 0.4rem;
            border-radius: 8px;
            transition: all 0.2s ease;
        }

        .draft-close:hover {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-primary);
        }

        .draft-form {
            display: grid;
            gap: 1rem;
        }

        .draft-field {
            display: grid;
            gap: 0.55rem;
        }

        .draft-label {
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .draft-textarea,
        .draft-input {
            width: 100%;
            border-radius: 12px;
            border: 1px solid var(--border-bright);
            background: var(--bg-card);
            color: var(--text-primary);
            padding: 0.95rem 1rem;
            font: inherit;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .draft-textarea {
            min-height: 180px;
            resize: vertical;
            line-height: 1.6;
        }

        .draft-textarea:focus,
        .draft-input:focus {
            outline: none;
            border-color: var(--accent-cyan);
            box-shadow: 0 0 0 3px rgba(0, 212, 255, 0.12);
        }

        .draft-meta-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1rem;
        }

        .draft-platform-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.75rem;
        }

        .draft-platform-option {
            display: flex;
            align-items: center;
            gap: 0.7rem;
            padding: 0.85rem 0.95rem;
            border-radius: 12px;
            border: 1px solid var(--border-bright);
            background: var(--bg-card);
        }

        .draft-platform-option input {
            width: 16px;
            height: 16px;
            accent-color: var(--accent-cyan);
        }

        .draft-platform-copy {
            display: flex;
            flex-direction: column;
            gap: 0.18rem;
        }

        .draft-platform-name {
            color: var(--text-primary);
            font-weight: 600;
        }

        .draft-platform-hint {
            color: var(--text-secondary);
            font-size: 0.8rem;
        }

        .draft-upload-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            align-items: center;
        }

        .draft-upload-btn {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            border: 1px solid var(--border-bright);
            background: rgba(0, 212, 255, 0.08);
            color: var(--accent-cyan);
            border-radius: 12px;
            padding: 0.8rem 1rem;
            font-weight: 600;
            cursor: pointer;
        }

        .draft-upload-btn:hover {
            background: rgba(0, 212, 255, 0.14);
        }

        .draft-upload-note {
            color: var(--text-secondary);
            font-size: 0.82rem;
        }

        .draft-file-input {
            display: none;
        }

        .draft-attachment-preview {
            display: none;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            padding: 0.85rem 1rem;
            border-radius: 12px;
            border: 1px solid rgba(0, 212, 255, 0.16);
            background: rgba(0, 212, 255, 0.06);
        }

        .draft-attachment-preview.show {
            display: flex;
        }

        .draft-attachment-name {
            color: var(--text-primary);
            font-weight: 500;
        }

        .draft-attachment-remove {
            border: none;
            background: none;
            color: #f87171;
            cursor: pointer;
            font-weight: 600;
        }

        .draft-helper {
            color: var(--text-secondary);
            font-size: 0.82rem;
            line-height: 1.5;
        }

        .draft-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            margin-top: 0.5rem;
        }

        .draft-footer-note {
            color: var(--text-secondary);
            font-size: 0.82rem;
        }

        .draft-actions {
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            justify-content: flex-end;
        }

        .draft-btn {
            border: none;
            border-radius: 12px;
            padding: 0.9rem 1.15rem;
            font-weight: 600;
            cursor: pointer;
        }

        .draft-btn-secondary {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-primary);
            border: 1px solid var(--border-bright);
        }

        .draft-btn-primary {
            background: linear-gradient(135deg, #00d4ff, #0ea5e9);
            color: #031018;
            box-shadow: 0 12px 30px rgba(0, 212, 255, 0.2);
        }

        .draft-btn-primary:disabled {
            opacity: 0.65;
            cursor: wait;
        }

        @media (max-width: 720px) {
            .draft-meta-grid,
            .draft-platform-grid {
                grid-template-columns: 1fr;
            }

            .draft-footer {
                flex-direction: column;
                align-items: stretch;
            }

            .draft-actions {
                width: 100%;
            }

            .draft-btn {
                width: 100%;
            }
        }

        /* Upload Modal */
        .upload-modal {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.9);
            backdrop-filter: blur(12px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            opacity: 0;
            transition: opacity 0.25s ease;
        }

        .upload-modal.show {
            display: flex;
            opacity: 1;
        }

        .upload-panel {
            background: var(--bg-elevated);
            border: 1px solid var(--border-bright);
            border-radius: 16px;
            width: 100%;
            max-width: 500px;
            padding: 2rem;
            transform: scale(0.95) translateY(10px);
            transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .upload-modal.show .upload-panel {
            transform: scale(1) translateY(0);
        }

        .upload-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }

        .upload-title {
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .upload-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.25rem;
            line-height: 1;
            border-radius: 6px;
            transition: all 0.2s;
        }

        .upload-close:hover {
            background: rgba(255,255,255,0.08);
            color: var(--text-primary);
        }

        .upload-zone {
            border: 2px dashed var(--border-bright);
            border-radius: 12px;
            padding: 3rem 2rem;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            background: var(--bg-card);
        }

        .upload-zone:hover,
        .upload-zone.drag-over {
            border-color: var(--accent-cyan);
            background: rgba(0, 212, 255, 0.05);
        }

        .upload-zone.drag-over {
            transform: scale(1.02);
            box-shadow: 0 0 30px rgba(0, 212, 255, 0.15);
        }

        .upload-icon {
            width: 48px;
            height: 48px;
            margin: 0 auto 1rem;
            color: var(--accent-cyan);
            opacity: 0.8;
        }

        .upload-text {
            color: var(--text-secondary);
            font-size: 0.95rem;
            margin-bottom: 0.5rem;
        }

        .upload-hint {
            color: var(--text-muted);
            font-size: 0.8rem;
        }

        .upload-input {
            display: none;
        }

        .upload-file-info {
            display: none;
            align-items: center;
            gap: 1rem;
            padding: 1rem;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-top: 1rem;
        }

        .upload-file-info.show {
            display: flex;
        }

        .file-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--accent-purple), var(--accent-cyan));
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 600;
            font-size: 0.7rem;
            font-family: 'JetBrains Mono', monospace;
        }

        .file-details {
            flex: 1;
        }

        .file-name {
            font-weight: 500;
            color: var(--text-primary);
            font-size: 0.9rem;
            margin-bottom: 0.2rem;
        }

        .file-size {
            color: var(--text-muted);
            font-size: 0.75rem;
            font-family: 'JetBrains Mono', monospace;
        }

        .file-remove {
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            padding: 0.5rem;
            border-radius: 6px;
            transition: all 0.2s;
        }

        .file-remove:hover {
            background: rgba(248, 113, 113, 0.15);
            color: var(--accent-red);
        }

        .upload-actions {
            display: flex;
            gap: 0.75rem;
            margin-top: 1.5rem;
        }

        .btn-upload {
            flex: 1;
            padding: 0.875rem 1.5rem;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-purple));
            border: none;
            border-radius: 10px;
            color: white;
            font-family: 'Outfit', sans-serif;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
        }

        .btn-upload:hover:not(:disabled) {
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(0, 212, 255, 0.3);
        }

        .btn-upload:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .btn-cancel {
            padding: 0.875rem 1.5rem;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text-secondary);
            font-family: 'Outfit', sans-serif;
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-cancel:hover {
            border-color: var(--border-bright);
            color: var(--text-primary);
        }

        /* Processing State */
        .upload-processing {
            display: none;
            text-align: center;
            padding: 2rem;
        }

        .upload-processing.show {
            display: block;
        }

        .processing-spinner {
            width: 48px;
            height: 48px;
            margin: 0 auto 1.5rem;
            border: 3px solid var(--border);
            border-top-color: var(--accent-cyan);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .processing-text {
            color: var(--text-primary);
            font-size: 1rem;
            margin-bottom: 0.5rem;
        }

        .processing-subtext {
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        /* Result State */
        .upload-result {
            display: none;
            text-align: center;
            padding: 2rem;
        }

        .upload-result.show {
            display: block;
        }

        .result-icon {
            width: 56px;
            height: 56px;
            margin: 0 auto 1rem;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .result-icon.success {
            background: rgba(74, 222, 128, 0.15);
            color: var(--accent-green);
        }

        .result-icon.error {
            background: rgba(248, 113, 113, 0.15);
            color: var(--accent-red);
        }

        .result-title {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 0.5rem;
        }

        .result-stats {
            display: flex;
            justify-content: center;
            gap: 2rem;
            margin: 1.5rem 0;
        }

        .result-stat {
            text-align: center;
        }

        .result-stat-num {
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.75rem;
            font-weight: 600;
            color: var(--accent-cyan);
        }

        .result-stat-label {
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .btn-done {
            padding: 0.875rem 2rem;
            background: linear-gradient(135deg, var(--accent-green), #22c55e);
            border: none;
            border-radius: 10px;
            color: white;
            font-family: 'Outfit', sans-serif;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-done:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(74, 222, 128, 0.3);
        }

        /* Mobile Responsive */
        @media (max-width: 1024px) {
            .kanban {
                grid-template-columns: repeat(2, 1fr);
                height: auto;
                min-height: calc(100vh - 65px);
            }
            .column { min-height: 300px; }
        }

        @media (max-width: 768px) {
            body {
                overflow: auto;
                overflow-x: hidden;
            }
            .header {
                flex-direction: column;
                gap: 0.75rem;
                padding: 1rem;
                position: sticky;
                top: 0;
                z-index: 100;
            }
            .stats-bar {
                gap: 1rem;
                flex-wrap: wrap;
                justify-content: center;
            }
            .kanban {
                grid-template-columns: 1fr;
                padding: 0.75rem;
                gap: 0.75rem;
                height: auto;
                overflow: visible;
                padding-bottom: 2rem;
            }
            .column { min-height: 250px; max-height: 400px; }
            .column-body { min-height: 150px; }

            .modal {
                width: 95%;
                max-height: 90vh;
                margin: 5vh auto;
            }
            .modal-content {
                font-size: 1rem;
                max-height: 200px;
            }
            .modal-actions {
                flex-wrap: wrap;
                gap: 0.5rem;
            }
            .modal-actions a, .modal-actions button {
                flex: 1 1 45%;
                min-width: 120px;
                text-align: center;
            }

            .image-generator-modal .img-gen-panel {
                width: 95%;
                max-width: none;
                max-height: 90vh;
            }
            .img-gen-content {
                flex-direction: column;
            }
            .img-preview-container {
                max-width: 100%;
            }
            #tweetCanvas {
                max-width: 100%;
                height: auto;
            }
            .img-gen-controls {
                padding: 1rem;
            }
            .theme-options, .dimension-options {
                flex-wrap: wrap;
            }
            .share-buttons {
                flex-wrap: wrap;
            }
            .share-buttons button {
                flex: 1 1 45%;
            }

            .upload-panel {
                width: 95%;
                max-height: 90vh;
            }
        }

        @media (max-width: 480px) {
            .logo { font-size: 1.1rem; }
            .stats-bar { font-size: 0.75rem; gap: 0.75rem; }
            .stat-label { font-size: 0.65rem; }

            .card { padding: 0.75rem; }
            .card-content { font-size: 0.85rem; }

            .column-title { font-size: 0.75rem; }
            .column-count { font-size: 0.65rem; }

            .modal-actions a, .modal-actions button {
                flex: 1 1 100%;
                padding: 0.75rem;
            }

            .btn-post-x, .btn-generate-img, .btn-post-instagram, .btn-post-facebook, .btn-post-linkedin {
                padding: 0.75rem 1rem;
                font-size: 0.9rem;
            }
        }

        /* Touch-friendly adjustments */
        @media (hover: none) and (pointer: coarse) {
            .card {
                padding: 1rem;
                margin-bottom: 0.75rem;
            }
            .btn-post-x, .btn-generate-img, .btn-post-instagram, .btn-post-facebook, .btn-post-linkedin,
            .btn-share, .theme-btn, .dim-btn {
                min-height: 44px;
                min-width: 44px;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">Social Kanban</div>
        <div class="stats-bar">
            <div class="stat"><span class="stat-num" id="stat-pending">{{ stats.pending }}</span><span class="stat-label">review</span></div>
            <button class="btn-create" id="newDraftButton" type="button">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
                    <path d="M12 5v14M5 12h14"/>
                </svg>
                New Draft
            </button>
            <div class="stat"><span class="stat-num" id="stat-scheduled">{{ stats.scheduled }}</span><span class="stat-label">scheduled</span></div>
            <div class="stat"><span class="stat-num" id="stat-posted">{{ stats.posted }}</span><span class="stat-label">posted</span></div>
        </div>
    </div>

    <div class="kanban">
        <div class="column col-pending" data-status="pending">
            <div class="column-header">
                <span class="column-title">Pending Review</span>
                <span class="column-count">{{ pending_posts|length }}</span>
            </div>
            <div class="column-body">
                {% for post in pending_posts %}
                <div class="card pending" data-type="post" data-id="{{ post.id }}" data-platform="{{ post.platform }}" data-platform-label="{{ platform_label(post.platform) }}" data-scheduled-at="{{ post.scheduled_time.isoformat() if post.scheduled_time else '' }}" data-schedule-label="{{ post.scheduled_time.strftime('%b %d, %I:%M %p') if post.scheduled_time else '' }}" data-image-url="{{ post.media_path or '' }}" data-full-content='{{ post.content | tojson | safe }}'>
                    <div class="card-content">{{ post.content[:140] }}{% if post.content|length > 140 %}...{% endif %}</div>
                    <div class="card-meta post-meta">
                        <span><span class="status-dot pending"></span><span class="card-status-text">{{ platform_label(post.platform) }} • {{ post.scheduled_time.strftime('%b %d, %I:%M %p') if post.scheduled_time else 'Needs review' }}</span></span>
                        <span class="char-count {{ 'char-ok' if post.content|length <= 250 else 'char-warn' if post.content|length <= 280 else 'char-over' }}">{{ post.content|length }}/280</span>
                    </div>
                    {% if post.media_path %}
                    <div class="attachment-chip">Image attached</div>
                    {% endif %}
                </div>
                {% else %}
                <div class="empty-state">Agent and manual drafts land here first</div>
                {% endfor %}
            </div>
        </div>

        <div class="column col-approved" data-status="approved">
            <div class="column-header">
                <span class="column-title">Scheduled</span>
                <span class="column-count">{{ scheduled_posts|length }}</span>
            </div>
            <div class="column-body">
                {% for post in scheduled_posts %}
                <div class="card approved" data-type="post" data-id="{{ post.id }}" data-platform="{{ post.platform }}" data-platform-label="{{ platform_label(post.platform) }}" data-scheduled-at="{{ post.scheduled_time.isoformat() if post.scheduled_time else '' }}" data-schedule-label="{{ post.scheduled_time.strftime('%b %d, %I:%M %p') if post.scheduled_time else '' }}" data-image-url="{{ post.media_path or '' }}" data-full-content='{{ post.content | tojson | safe }}'>
                    <div class="card-content">{{ post.content[:140] }}{% if post.content|length > 140 %}...{% endif %}</div>
                    <div class="card-meta post-meta">
                        <span><span class="status-dot approved"></span><span class="card-status-text">{{ platform_label(post.platform) }} • {{ post.scheduled_time.strftime('%b %d, %I:%M %p') if post.scheduled_time else 'Ready to post' }}</span></span>
                        <span class="char-count {{ 'char-ok' if post.content|length <= 250 else 'char-warn' if post.content|length <= 280 else 'char-over' }}">{{ post.content|length }}/280</span>
                    </div>
                    {% if post.media_path %}
                    <div class="attachment-chip">Image attached</div>
                    {% endif %}
                </div>
                {% else %}
                <div class="empty-state">Approve drafts to queue them here</div>
                {% endfor %}
            </div>
        </div>

        <div class="column col-posted" data-status="posted">
            <div class="column-header">
                <span class="column-title">Posted</span>
                <span class="column-count">{{ posted_posts|length }}</span>
            </div>
            <div class="column-body">
                {% for post in posted_posts %}
                <div class="card posted" data-type="post" data-id="{{ post.id }}" data-platform="{{ post.platform }}" data-platform-label="{{ platform_label(post.platform) }}" data-scheduled-at="{{ post.scheduled_time.isoformat() if post.scheduled_time else '' }}" data-posted-label="{{ post.posted_time.strftime('%b %d, %I:%M %p') if post.posted_time else '' }}" data-image-url="{{ post.media_path or '' }}" data-full-content='{{ post.content | tojson | safe }}'>
                    <div class="card-content">{{ post.content[:100] }}{% if post.content|length > 100 %}...{% endif %}</div>
                    <div class="card-meta post-meta">
                        <span><span class="status-dot posted"></span><span class="card-status-text">{{ platform_label(post.platform) }} • {{ post.posted_time.strftime('%b %d, %I:%M %p') if post.posted_time else 'Done' }}</span></span>
                    </div>
                    {% if post.media_path %}
                    <div class="attachment-chip">Image attached</div>
                    {% endif %}
                </div>
                {% else %}
                <div class="empty-state">Nothing posted yet</div>
                {% endfor %}
            </div>
        </div>
    </div>

    <div class="draft-modal" id="draftModal">
        <div class="draft-panel">
            <div class="draft-header">
                <div class="draft-title-wrap">
                    <div class="draft-title">Create Draft</div>
                    <div class="draft-subtitle">Write the post once, choose socials, and keep it in review until you are ready.</div>
                </div>
                <button class="draft-close" onclick="closeDraftModal()">&times;</button>
            </div>

            <div class="draft-form">
                <div class="draft-field">
                    <label class="draft-label" for="draftContent">Post Content</label>
                    <textarea class="draft-textarea" id="draftContent" placeholder="Write the text for your post. Platform-specific cards will be created from this content." maxlength="5000"></textarea>
                    <div class="draft-helper">Use this for the base copy. Platform-specific drafts will be created in Pending Review so you can approve them before posting.</div>
                </div>

                <div class="draft-field">
                    <span class="draft-label">Platforms</span>
                    <div class="draft-platform-grid">
                        <label class="draft-platform-option">
                            <input type="checkbox" name="draftPlatform" value="twitter" checked>
                            <span class="draft-platform-copy">
                                <span class="draft-platform-name">X / Twitter</span>
                                <span class="draft-platform-hint">Short-form post draft</span>
                            </span>
                        </label>
                        <label class="draft-platform-option">
                            <input type="checkbox" name="draftPlatform" value="linkedin">
                            <span class="draft-platform-copy">
                                <span class="draft-platform-name">LinkedIn</span>
                                <span class="draft-platform-hint">Company page post draft</span>
                            </span>
                        </label>
                        <label class="draft-platform-option">
                            <input type="checkbox" name="draftPlatform" value="facebook">
                            <span class="draft-platform-copy">
                                <span class="draft-platform-name">Facebook</span>
                                <span class="draft-platform-hint">Page post draft</span>
                            </span>
                        </label>
                        <label class="draft-platform-option">
                            <input type="checkbox" name="draftPlatform" value="instagram">
                            <span class="draft-platform-copy">
                                <span class="draft-platform-name">Instagram</span>
                                <span class="draft-platform-hint">Requires an image at publish time</span>
                            </span>
                        </label>
                    </div>
                </div>

                <div class="draft-meta-grid">
                    <div class="draft-field">
                        <label class="draft-label" for="draftSchedule">Target Schedule</label>
                        <input class="draft-input" id="draftSchedule" type="datetime-local">
                        <div class="draft-helper">Optional. This is a target publish time that stays with the draft when you move it into Scheduled.</div>
                    </div>

                    <div class="draft-field">
                        <label class="draft-label" for="draftImageUrl">Image URL</label>
                        <input class="draft-input" id="draftImageUrl" type="url" placeholder="https://...">
                        <div class="draft-helper">Paste an existing public image URL, or upload a local image below. Video attachments can come later.</div>
                    </div>
                </div>

                <div class="draft-field">
                    <span class="draft-label">Image Attachment</span>
                    <div class="draft-upload-row">
                        <button class="draft-upload-btn" type="button" onclick="selectDraftImage()">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                <polyline points="17 8 12 3 7 8"/>
                                <line x1="12" y1="3" x2="12" y2="15"/>
                            </svg>
                            Upload Image
                        </button>
                        <span class="draft-upload-note">Supports image files now. Video attachments can be added later.</span>
                    </div>
                    <input class="draft-file-input" id="draftImageFile" type="file" accept="image/*">
                    <div class="draft-attachment-preview" id="draftAttachmentPreview">
                        <span class="draft-attachment-name" id="draftAttachmentName"></span>
                        <button class="draft-attachment-remove" type="button" onclick="removeDraftImage()">Remove</button>
                    </div>
                </div>

                <div class="draft-footer">
                    <div class="draft-footer-note">Drafts are created in Pending Review. Move them to Scheduled when you are ready to queue publishing.</div>
                    <div class="draft-actions">
                        <button class="draft-btn draft-btn-secondary" type="button" onclick="closeDraftModal()">Cancel</button>
                        <button class="draft-btn draft-btn-primary" id="draftSubmitBtn" type="button" onclick="submitDraft()">Create Draft</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="modal">
        <div class="modal">
            <div class="modal-header">
                <span style="color: #e7e9ea; font-weight: 600;">Post Preview</span>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="x-post">
                <div class="x-header">
                    <div class="x-avatar">{% if profile.picture_url %}<img src="{{ profile.picture_url }}" alt="">{% else %}{{ profile.name[0] }}{% endif %}</div>
                    <div class="x-user-info">
                        <div class="x-name">{{ profile.name }}</div>
                        <div class="x-handle">{{ profile.handle }}</div>
                    </div>
                </div>
                <div class="x-content" id="modal-content"></div>
                <div class="x-meta">
                    <span id="modal-time"></span>
                    <span class="x-char-count" id="modal-chars"></span>
                </div>
                <div class="modal-draft-actions" id="modal-draft-actions" style="display:none;">
                    <button class="btn-draft-edit" id="btn-edit-draft" onclick="openEditDraftFromModal()">
                        Edit Draft
                    </button>
                    <button class="btn-draft-delete" id="btn-delete-draft" onclick="deleteDraftFromModal()">
                        Remove Draft
                    </button>
                </div>
            </div>
            <div class="modal-status">
                <span class="status-label" id="modal-status"></span>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    <button class="btn-post-x" id="btn-post-x" onclick="postToTwitterFromModal()" style="display:none;">
                        Post to 𝕏
                    </button>
                    <button class="btn-generate-img" id="btn-generate-img" onclick="openImageGenerator()" style="display:none;">
                        📸 Create Image
                    </button>
                    <button class="btn-post-instagram" id="btn-post-instagram" onclick="postToInstagramFromModal()" style="display:none;">
                        📷 Instagram
                    </button>
                    <button class="btn-post-facebook" id="btn-post-facebook" onclick="postToFacebookFromModal()" style="display:none;">
                        📘 Facebook
                    </button>
                    <button class="btn-post-linkedin" id="btn-post-linkedin" onclick="postToLinkedInFromModal()" style="display:none;">
                        💼 LinkedIn
                    </button>
                </div>
                <span id="modal-source" style="color: #71767b; font-size: 0.85rem;"></span>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <!-- Image Generator Modal -->
    <div class="image-generator-modal" id="imageGenModal">
        <div class="img-gen-panel">
            <div class="img-gen-header">
                <span class="img-gen-title">Generate Image for Socials</span>
                <button class="img-gen-close" onclick="closeImageGenerator()">&times;</button>
            </div>
            <div class="img-gen-content">
                <div class="img-preview-container">
                    <canvas id="tweetCanvas" width="1080" height="1080"></canvas>
                </div>
                <div class="img-gen-controls">
                    <div class="control-group">
                        <span class="control-label">Theme</span>
                        <div class="theme-options">
                            <button class="theme-btn brand active" data-theme="brand" onclick="setTheme('brand')">Brand</button>
                            <button class="theme-btn minimal" data-theme="minimal" onclick="setTheme('minimal')">Minimal</button>
                            <button class="theme-btn bold" data-theme="bold" onclick="setTheme('bold')">Bold</button>
                            <button class="theme-btn dark" data-theme="dark" onclick="setTheme('dark')">Dark</button>
                            <button class="theme-btn edge" data-theme="edge" onclick="setTheme('edge')">Edge</button>
                            <button class="theme-btn stoic" data-theme="stoic" onclick="setTheme('stoic')">Stoic</button>
                        </div>
                    </div>
                    <div class="control-group">
                        <span class="control-label">Dimension</span>
                        <div class="dimension-options">
                            <button class="dim-btn active" data-dim="square" onclick="setDimension('square')">Square</button>
                            <button class="dim-btn" data-dim="story" onclick="setDimension('story')">Story</button>
                            <button class="dim-btn" data-dim="wide" onclick="setDimension('wide')">Wide</button>
                        </div>
                    </div>
                    <div class="img-gen-actions">
                        <button class="btn-download" onclick="downloadImage()">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                <polyline points="7 10 12 15 17 10"/>
                                <line x1="12" y1="15" x2="12" y2="3"/>
                            </svg>
                            Download PNG
                        </button>
                        <div class="share-buttons">
                            <button class="btn-share instagram" onclick="openInstagram()">📷 Instagram</button>
                            <button class="btn-share linkedin" onclick="openLinkedIn()">💼 LinkedIn</button>
                            <button class="btn-share facebook" onclick="postToFacebook()">📘 Facebook</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Upload Modal -->
    <div class="upload-modal" id="uploadModal">
        <div class="upload-panel">
            <div class="upload-header">
                <span class="upload-title">Create Quotes from Document</span>
                <button class="upload-close" onclick="closeUploadModal()">&times;</button>
            </div>

            <div id="uploadForm">
                <div class="upload-zone" id="uploadZone">
                    <svg class="upload-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                        <polyline points="17 8 12 3 7 8"/>
                        <line x1="12" y1="3" x2="12" y2="15"/>
                    </svg>
                    <div class="upload-text">Drop your document here or click to browse</div>
                    <div class="upload-hint">Supports PDF and DOCX files</div>
                    <input type="file" class="upload-input" id="fileInput" accept=".pdf,.docx">
                </div>

                <div class="upload-file-info" id="fileInfo">
                    <div class="file-icon" id="fileExt">PDF</div>
                    <div class="file-details">
                        <div class="file-name" id="fileName">document.pdf</div>
                        <div class="file-size" id="fileSize">2.4 MB</div>
                    </div>
                    <button class="file-remove" onclick="removeFile()">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M18 6L6 18M6 6l12 12"/>
                        </svg>
                    </button>
                </div>

                <div class="upload-actions">
                    <button class="btn-cancel" onclick="closeUploadModal()">Cancel</button>
                    <button class="btn-upload" id="btnExtract" onclick="extractQuotes()" disabled>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
                        </svg>
                        Extract Quotes
                    </button>
                </div>
            </div>

            <div class="upload-processing" id="uploadProcessing">
                <div class="processing-spinner"></div>
                <div class="processing-text">Extracting quotes...</div>
                <div class="processing-subtext">This may take a minute depending on document size</div>
            </div>

            <div class="upload-result" id="uploadResult">
                <div class="result-icon success" id="resultIcon">
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                        <polyline points="20 6 9 17 4 12"/>
                    </svg>
                </div>
                <div class="result-title" id="resultTitle">Quotes Extracted!</div>
                <div class="result-stats">
                    <div class="result-stat">
                        <div class="result-stat-num" id="resultExtracted">0</div>
                        <div class="result-stat-label">Extracted</div>
                    </div>
                    <div class="result-stat">
                        <div class="result-stat-num" id="resultSaved">0</div>
                        <div class="result-stat-label">New Saved</div>
                    </div>
                </div>
                <button class="btn-done" onclick="finishUpload()">Done</button>
            </div>
        </div>
    </div>

    <!-- Stoic Card Modal -->
    <div class="stoic-modal" id="stoicModal">
        <div class="stoic-panel">
            <div class="stoic-header">
                <span class="stoic-title">
                    <span class="stoic-title-icon">&#9765;</span>
                    Daily Stoic Card
                </span>
                <button class="stoic-close" onclick="closeStoicModal()">&times;</button>
            </div>
            <div class="stoic-body" id="stoicBody">
                <div class="stoic-date-info">
                    <div class="stoic-date" id="stoicDate">Loading...</div>
                    <div class="stoic-entry-title" id="stoicEntryTitle"></div>
                    <div class="stoic-author" id="stoicAuthor"></div>
                </div>
                <div id="stoicContent">
                    <div class="stoic-loading" id="stoicInitial">
                        <p style="color: var(--text-secondary); margin-bottom: 1rem;">Generate today's Stoic card</p>
                    </div>
                </div>
            </div>
            <div class="stoic-actions" id="stoicActions">
                <button class="stoic-btn stoic-btn-cancel" onclick="closeStoicModal()">Cancel</button>
                <button class="stoic-btn stoic-btn-generate" id="btnStoicGenerate" onclick="generateStoicCard()">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
                    </svg>
                    Generate Card
                </button>
            </div>
        </div>
    </div>

    <script>
    const PROFILE = {
        pictureUrl: '{{ profile.picture_url }}',
        name: '{{ profile.name }}',
        handle: '{{ profile.handle }}'
    };
    const BRAND_CONFIG = {{ brand_config | tojson | safe }};
    let profileImageLoaded = null;
    if (PROFILE.pictureUrl) {
        profileImageLoaded = new Promise((resolve) => {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            img.onload = () => resolve(img);
            img.onerror = () => resolve(null);
            img.src = PROFILE.pictureUrl;
        });
    }

    (function() {
        'use strict';

        // Drag state
        let dragState = null;
        let ghostEl = null;
        let ghostPos = { x: 0, y: 0 };
        let targetPos = { x: 0, y: 0 };
        let velocity = { x: 0, y: 0 };
        let animationId = null;
        let lastFrameTime = 0;

        function showToast(msg, isError) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast show' + (isError ? ' error' : '');
            setTimeout(() => t.className = 'toast', 2500);
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('show');
        }

        function escapeHtml(value) {
            return String(value || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function platformDisplayLabel(platform) {
            return {
                twitter: 'X / Twitter',
                facebook: 'Facebook',
                instagram: 'Instagram',
                linkedin: 'LinkedIn',
            }[platform] || (platform ? platform.replace(/-/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase()) : 'Unknown');
        }

        function formatScheduleLabel(value) {
            if (!value) return '';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '';
            return date.toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
            });
        }

        function statusDisplayLabel(status) {
            return {
                pending: 'Pending Review',
                approved: 'Scheduled',
                posted: 'Posted',
                quote: 'Quote',
            }[status] || status;
        }

        function buildCardStatusText(platformLabel, status, scheduleLabel, postedLabel) {
            if (status === 'pending') {
                return scheduleLabel ? `${platformLabel} • ${scheduleLabel}` : `${platformLabel} • Needs review`;
            }
            if (status === 'approved') {
                return scheduleLabel ? `${platformLabel} • ${scheduleLabel}` : `${platformLabel} • Ready to post`;
            }
            if (status === 'posted') {
                return postedLabel ? `${platformLabel} • ${postedLabel}` : `${platformLabel} • Posted`;
            }
            return platformLabel;
        }

        function syncBoardStats() {
            const pending = document.querySelectorAll('.col-pending .card').length;
            const scheduled = document.querySelectorAll('.col-approved .card').length;
            const posted = document.querySelectorAll('.col-posted .card').length;
            const pendingEl = document.getElementById('stat-pending');
            const scheduledEl = document.getElementById('stat-scheduled');
            const postedEl = document.getElementById('stat-posted');
            if (pendingEl) pendingEl.textContent = pending;
            if (scheduledEl) scheduledEl.textContent = scheduled;
            if (postedEl) postedEl.textContent = posted;

            const pendingCount = document.querySelector('.col-pending .column-count');
            const scheduledCount = document.querySelector('.col-approved .column-count');
            const postedCount = document.querySelector('.col-posted .column-count');
            if (pendingCount) pendingCount.textContent = pending;
            if (scheduledCount) scheduledCount.textContent = scheduled;
            if (postedCount) postedCount.textContent = posted;

            [
                ['.col-pending .column-body', 'Agent and manual drafts land here first'],
                ['.col-approved .column-body', 'Approve drafts to queue them here'],
                ['.col-posted .column-body', 'Nothing posted yet'],
            ].forEach(([selector, message]) => {
                const body = document.querySelector(selector);
                if (!body) return;
                const hasCards = body.querySelector('.card');
                const emptyState = body.querySelector('.empty-state');
                if (hasCards && emptyState) {
                    emptyState.remove();
                } else if (!hasCards && !emptyState) {
                    const el = document.createElement('div');
                    el.className = 'empty-state';
                    el.textContent = message;
                    body.appendChild(el);
                }
            });
        }

        function updateCardPresentation(card, status) {
            const platformLabel = card.dataset.platformLabel || platformDisplayLabel(card.dataset.platform);
            const scheduleLabel = card.dataset.scheduleLabel || '';
            const postedLabel = card.dataset.postedLabel || '';
            const statusText = card.querySelector('.card-status-text');
            if (statusText) {
                statusText.textContent = buildCardStatusText(platformLabel, status, scheduleLabel, postedLabel);
            }
            const dot = card.querySelector('.status-dot');
            if (dot) {
                dot.classList.remove('pending', 'approved', 'posted');
                dot.classList.add(status);
            }
        }

        // Get full content from data attribute (JSON encoded)
        function getFullContent(card) {
            const fullContent = card.dataset.fullContent;
            if (fullContent) {
                try {
                    // Content is JSON-encoded, parse it
                    return JSON.parse(fullContent);
                } catch (e) {
                    // Fallback: decode HTML entities
                    const txt = document.createElement('textarea');
                    txt.innerHTML = fullContent;
                    return txt.value;
                }
            }
            return null;
        }

        function formatPreviewContent(content) {
            const escaped = escapeHtml(String(content || '')).replace(/\\n/g, '<br>');
            return escaped.replace(/(^|[\s>])#([A-Za-z0-9_]+)/g, (match, prefix, tag) => (
                `${prefix}<span class="x-hashtag">#${tag}</span>`
            ));
        }

        function toDateTimeLocalValue(value) {
            if (!value) return '';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '';
            const year = date.getFullYear();
            const month = String(date.getMonth() + 1).padStart(2, '0');
            const day = String(date.getDate()).padStart(2, '0');
            const hours = String(date.getHours()).padStart(2, '0');
            const minutes = String(date.getMinutes()).padStart(2, '0');
            return `${year}-${month}-${day}T${hours}:${minutes}`;
        }

        function buildPostCardMarkup(post) {
            const status = post.status || 'pending';
            const content = String(post.content || '');
            const platform = post.platform || 'twitter';
            const platformLabel = platformDisplayLabel(platform);
            const scheduleLabel = formatScheduleLabel(post.scheduled_at || post.scheduled_time || '');
            const imageUrl = post.media_url || post.media_path || '';
            const truncated = content.length > 140 ? content.substring(0, 140) + '...' : content;
            const charClass = content.length <= 250 ? 'char-ok' : content.length <= 280 ? 'char-warn' : 'char-over';
            const attachment = imageUrl ? '<div class="attachment-chip">Image attached</div>' : '';

            return {
                status,
                content,
                platform,
                platformLabel,
                scheduleLabel,
                scheduledAt: post.scheduled_at || post.scheduled_time || '',
                postedLabel: post.posted_at || post.posted_time ? formatScheduleLabel(post.posted_at || post.posted_time) : '',
                imageUrl,
                html: `
                    <div class="card-content">${escapeHtml(truncated)}</div>
                    <div class="card-meta post-meta">
                        <span><span class="status-dot ${status}"></span><span class="card-status-text">${escapeHtml(buildCardStatusText(platformLabel, status, scheduleLabel, ''))}</span></span>
                        <span class="char-count ${charClass}">${content.length}/280</span>
                    </div>
                    ${attachment}
                `,
            };
        }

        function applyPostCardData(card, post) {
            const cardData = buildPostCardMarkup(post);
            card.className = 'card ' + cardData.status;
            card.dataset.type = 'post';
            card.dataset.id = post.id;
            card.dataset.platform = cardData.platform;
            card.dataset.platformLabel = cardData.platformLabel;
            card.dataset.scheduledAt = cardData.scheduledAt;
            card.dataset.scheduleLabel = cardData.scheduleLabel;
            card.dataset.postedLabel = cardData.postedLabel;
            card.dataset.imageUrl = cardData.imageUrl;
            card.dataset.fullContent = JSON.stringify(cardData.content);
            card.innerHTML = cardData.html;
        }

        function openModal(card) {
            const type = card.dataset.type;
            const status = card.classList.contains('posted') ? 'posted' :
                           card.classList.contains('approved') ? 'approved' :
                           card.classList.contains('pending') ? 'pending' : 'quote';
            const imageUrl = card.dataset.imageUrl || null;
            const platform = card.dataset.platform || '';
            const platformLabel = card.dataset.platformLabel || platformDisplayLabel(platform);
            const scheduleLabel = card.dataset.scheduleLabel || '';
            const scheduledAt = card.dataset.scheduledAt || '';
            const postedLabel = card.dataset.postedLabel || '';

            let content;
            if (type === 'post') {
                // Get full content from data attribute
                content = getFullContent(card);
                if (!content) {
                    // Fallback to card text (shouldn't happen but just in case)
                    content = card.querySelector('.card-content').textContent;
                }
            } else {
                // Quote - get full content and format as post preview
                const quoteText = getFullContent(card) || card.querySelector('.card-content').textContent.replace(/^"|"$/g, '');
                content = '"' + quoteText + '"\\n\\n' + (BRAND_CONFIG.tagline || '') + '\\n\\n' + (BRAND_CONFIG.hashtags || '');
            }
            content = String(content || '');

            let formattedContent = formatPreviewContent(content);

            // If post has an image (stoic card), show it above the content
            if (imageUrl) {
                formattedContent = '<img src="' + imageUrl + '" style="width:100%;border-radius:8px;margin-bottom:12px;">' + formattedContent;
            }

            document.getElementById('modal-content').innerHTML = formattedContent;
            document.getElementById('modal-chars').textContent = content.length + '/280';
            document.getElementById('modal-chars').className = 'x-char-count ' + (content.length <= 250 ? 'char-ok' : content.length <= 280 ? 'char-warn' : 'char-over');

            const statusEl = document.getElementById('modal-status');
            statusEl.className = 'status-label ' + status;
            statusEl.innerHTML = '<span class="status-dot ' + status + '"></span>' + statusDisplayLabel(status);

            const metaParts = [];
            if (platformLabel && type === 'post') metaParts.push(platformLabel);
            if (status === 'approved') {
                metaParts.push(scheduleLabel ? `Scheduled ${scheduleLabel}` : 'Ready to post');
            } else if (status === 'pending') {
                metaParts.push(scheduleLabel ? `Target ${scheduleLabel}` : 'Waiting for approval');
            } else if (status === 'posted') {
                metaParts.push(postedLabel ? `Posted ${postedLabel}` : 'Already posted');
            }
            if (imageUrl) metaParts.push('Image attached');
            document.getElementById('modal-source').textContent = metaParts.join(' • ');
            document.getElementById('modal-time').textContent = new Date().toLocaleString('en-US', {hour:'numeric', minute:'2-digit', month:'short', day:'numeric', year:'numeric'});

            // Show action buttons
            const postBtn = document.getElementById('btn-post-x');
            const imgBtn = document.getElementById('btn-generate-img');
            const igBtn = document.getElementById('btn-post-instagram');
            const fbBtn = document.getElementById('btn-post-facebook');
            const liBtn = document.getElementById('btn-post-linkedin');
            const draftActions = document.getElementById('modal-draft-actions');
            const editBtn = document.getElementById('btn-edit-draft');
            const deleteBtn = document.getElementById('btn-delete-draft');

            postBtn.style.display = 'none';
            imgBtn.style.display = 'none';
            igBtn.style.display = 'none';
            fbBtn.style.display = 'none';
            liBtn.style.display = 'none';
            draftActions.style.display = 'none';

            editBtn.dataset.postId = card.dataset.id || '';
            editBtn.dataset.platform = platform;
            editBtn.dataset.platformLabel = platformLabel;
            editBtn.dataset.content = content;
            editBtn.dataset.scheduledAt = scheduledAt;
            editBtn.dataset.imageUrl = imageUrl || '';
            editBtn.dataset.status = status;
            deleteBtn.dataset.postId = card.dataset.id || '';
            deleteBtn.dataset.status = status;

            if (type === 'post' && status === 'approved') {
                const wantsImageTools = ['instagram', 'facebook', 'linkedin'].includes(platform);
                draftActions.style.display = 'flex';
                if (platform === 'twitter') {
                    postBtn.style.display = 'block';
                    postBtn.dataset.postId = card.dataset.id || '';
                } else if (platform === 'instagram') {
                    igBtn.style.display = 'block';
                    igBtn.dataset.content = content;
                    igBtn.dataset.postId = card.dataset.id || '';
                    igBtn.dataset.imageUrl = imageUrl || '';
                    imgBtn.style.display = imageUrl ? 'none' : 'block';
                } else if (platform === 'facebook') {
                    fbBtn.style.display = 'block';
                    fbBtn.dataset.content = content;
                    fbBtn.dataset.postId = card.dataset.id || '';
                    fbBtn.dataset.imageUrl = imageUrl || '';
                    imgBtn.style.display = imageUrl ? 'none' : 'block';
                } else if (platform === 'linkedin') {
                    liBtn.style.display = 'block';
                    liBtn.dataset.content = content;
                    liBtn.dataset.postId = card.dataset.id || '';
                    liBtn.dataset.imageUrl = imageUrl || '';
                    imgBtn.style.display = imageUrl ? 'none' : 'block';
                } else if (wantsImageTools) {
                    imgBtn.style.display = imageUrl ? 'none' : 'block';
                }

                imgBtn.dataset.content = content;
                imgBtn.dataset.postId = card.dataset.id || '';
                imgBtn.dataset.imageUrl = imageUrl || '';
            } else if (type === 'post' && status === 'pending') {
                draftActions.style.display = 'flex';
            } else {
                postBtn.dataset.postId = '';
            }

            document.getElementById('modal').classList.add('show');
        }

        // Create a new post card element
        function createPostCard(post) {
            const card = document.createElement('div');
            applyPostCardData(card, post);
            return card;
        }

        document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

        // Buttery smooth 60fps animation with spring physics
        function animateGhost(timestamp) {
            if (!ghostEl || !dragState?.dragging) {
                animationId = null;
                return;
            }

            // Time-based interpolation for consistent speed across refresh rates
            const dt = lastFrameTime ? Math.min((timestamp - lastFrameTime) / 16.67, 2) : 1;
            lastFrameTime = timestamp;

            // Spring-damper physics for natural feel
            const stiffness = 0.35;
            const damping = 0.85;

            // Calculate spring force
            const dx = targetPos.x - ghostPos.x;
            const dy = targetPos.y - ghostPos.y;

            // Update velocity with spring force and damping
            velocity.x = (velocity.x + dx * stiffness * dt) * damping;
            velocity.y = (velocity.y + dy * stiffness * dt) * damping;

            // Update position
            ghostPos.x += velocity.x * dt;
            ghostPos.y += velocity.y * dt;

            // Smooth rotation based on horizontal velocity
            const rotation = Math.max(-3, Math.min(3, velocity.x * 0.08));

            // Use transform3d for GPU compositing
            ghostEl.style.transform = `translate3d(${Math.round(ghostPos.x * 10) / 10}px, ${Math.round(ghostPos.y * 10) / 10}px, 0) scale(1.03) rotate(${rotation.toFixed(2)}deg)`;

            animationId = requestAnimationFrame(animateGhost);
        }

        document.addEventListener('pointerdown', e => {
            const card = e.target.closest('.card');
            if (!card || e.button !== 0 || e.target.closest('.modal-overlay') || e.target.closest('.upload-modal') || e.target.closest('.draft-modal')) return;
            e.preventDefault();
            card.setPointerCapture(e.pointerId);
            const rect = card.getBoundingClientRect();
            dragState = {
                card,
                pointerId: e.pointerId,
                type: card.dataset.type,
                id: card.dataset.id,
                startX: e.clientX,
                startY: e.clientY,
                offX: e.clientX - rect.left,
                offY: e.clientY - rect.top,
                dragging: false
            };
            // Reset animation state
            velocity.x = 0;
            velocity.y = 0;
            lastFrameTime = 0;
        }, {passive: false});

        document.addEventListener('pointermove', e => {
            if (!dragState) return;
            const dist = Math.hypot(e.clientX - dragState.startX, e.clientY - dragState.startY);

            if (!dragState.dragging && dist > 5) {
                dragState.dragging = true;
                dragState.card.classList.add('is-dragging');
                document.body.classList.add('is-dragging');

                // Create ghost element
                const rect = dragState.card.getBoundingClientRect();
                ghostEl = dragState.card.cloneNode(true);
                ghostEl.className = 'card drag-ghost';
                ghostEl.style.cssText = `width:${rect.width}px;left:0;top:0;pointer-events:none;`;

                // Start ghost at current cursor position for immediate feedback
                const startX = e.clientX - dragState.offX;
                const startY = e.clientY - dragState.offY;
                ghostPos.x = startX;
                ghostPos.y = startY;
                targetPos.x = startX;
                targetPos.y = startY;

                ghostEl.style.transform = `translate3d(${ghostPos.x}px, ${ghostPos.y}px, 0) scale(1.03)`;
                document.body.appendChild(ghostEl);

                // Fade in ghost immediately
                requestAnimationFrame(() => {
                    ghostEl.classList.add('visible');
                    // Start animation loop
                    if (!animationId) animationId = requestAnimationFrame(animateGhost);
                });
            }

            if (!dragState.dragging) return;

            // Update target position (cursor position minus offset)
            targetPos.x = e.clientX - dragState.offX;
            targetPos.y = e.clientY - dragState.offY;

            // Highlight drop target (use ghostEl position to avoid flickering)
            document.querySelectorAll('.column').forEach(c => c.classList.remove('drag-over'));
            // Temporarily hide ghost to get element under cursor
            if (ghostEl) ghostEl.style.visibility = 'hidden';
            const el = document.elementFromPoint(e.clientX, e.clientY);
            if (ghostEl) ghostEl.style.visibility = '';
            const col = el?.closest('.column');
            if (col) col.classList.add('drag-over');
        });

        document.addEventListener('pointerup', async e => {
            if (!dragState) return;
            const wasDragging = dragState.dragging;
            const card = dragState.card;

            // Release pointer capture
            if (dragState.pointerId) {
                try { card.releasePointerCapture(dragState.pointerId); } catch(e) {}
            }

            // Stop animation loop
            if (animationId) {
                cancelAnimationFrame(animationId);
                animationId = null;
            }

            // Cleanup
            card.classList.remove('is-dragging');
            document.body.classList.remove('is-dragging');
            document.querySelectorAll('.column').forEach(c => c.classList.remove('drag-over'));

            if (!wasDragging) {
                openModal(card);
                dragState = null;
                return;
            }

            // Handle drop - temporarily hide ghost to find element under cursor
            if (ghostEl) ghostEl.style.visibility = 'hidden';
            const dropEl = document.elementFromPoint(e.clientX, e.clientY);
            if (ghostEl) ghostEl.style.visibility = '';
            const targetCol = dropEl?.closest('.column');
            const targetBody = targetCol?.querySelector('.column-body');
            const targetStatus = targetCol?.dataset.status;

            // Animate ghost to drop position
            if (ghostEl) {
                ghostEl.classList.add('dropping');
                let dropX, dropY;
                if (targetCol) {
                    const targetRect = targetBody.getBoundingClientRect();
                    dropX = targetRect.left + 8;
                    dropY = targetRect.top + 8;
                } else {
                    const cardRect = card.getBoundingClientRect();
                    dropX = cardRect.left;
                    dropY = cardRect.top;
                }
                ghostEl.style.transform = `translate3d(${dropX}px, ${dropY}px, 0) scale(0.98)`;
                setTimeout(() => { if (ghostEl) { ghostEl.remove(); ghostEl = null; } }, 250);
            }

            if (!targetCol) {
                dragState = null;
                return;
            }

            const sourceCol = card.closest('.column');

            try {
                const endpoint = '/api/post/status';
                const body = {post_id: dragState.id, status: targetStatus};

                const resp = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });

                if (resp.ok) {
                    card.style.transition = 'opacity 0.15s ease';
                    card.style.opacity = '0';

                    setTimeout(() => {
                        card.classList.remove('pending', 'approved', 'posted');
                        card.classList.add(targetStatus);
                        if (targetStatus !== 'posted') {
                            card.dataset.postedLabel = '';
                        } else {
                            card.dataset.postedLabel = new Date().toLocaleString('en-US', {
                                month: 'short',
                                day: 'numeric',
                                hour: 'numeric',
                                minute: '2-digit',
                            });
                        }
                        const targetEmptyState = targetBody.querySelector('.empty-state');
                        if (targetEmptyState) targetEmptyState.remove();
                        targetBody.appendChild(card);
                        updateCardPresentation(card, targetStatus);

                        [sourceCol, targetCol].forEach(col => {
                            const cnt = col.querySelectorAll('.card').length;
                            const countEl = col.querySelector('.column-count');
                            if (countEl) countEl.textContent = cnt;
                        });
                        syncBoardStats();

                        requestAnimationFrame(() => {
                            card.style.opacity = '1';
                            setTimeout(() => card.style.transition = '', 200);
                        });
                    }, 150);

                    showToast('Moved to ' + statusDisplayLabel(targetStatus));
                } else {
                    showToast('Failed', true);
                }
            } catch (err) {
                showToast('Error', true);
            }

            dragState = null;
        });

        document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
        document.addEventListener('dragstart', e => e.preventDefault());

        Object.assign(window, {
            closeModal,
            showToast,
            createPostCard,
            syncBoardStats,
            updateCardPresentation,
        });
    })();

    const createPostCard = (...args) => window.createPostCard(...args);
    const syncBoardStats = (...args) => window.syncBoardStats(...args);
    const updateCardPresentation = (...args) => window.updateCardPresentation(...args);

    // Image Generator
    let imgGenState = {
        content: '',
        theme: 'brand',
        dimension: 'square'
    };

    const themes = {
        brand: { bg: '#1a3a2f', text: '#f5f0e6', secondary: '#a8b5a0', accent: '#c9a227', card: 'rgba(255,255,255,0.08)', style: 'card' },
        minimal: { bg: '#fafafa', text: '#1a1a1a', secondary: '#666666', accent: '#1a3a2f', card: '#ffffff', style: 'centered' },
        bold: { bg: 'gradient-brand', text: '#f5f0e6', secondary: '#c9a227', accent: '#c9a227', card: 'none', style: 'fullbleed' },
        dark: { bg: '#0d1117', text: '#e6edf3', secondary: '#8b949e', accent: '#c9a227', card: 'rgba(255,255,255,0.06)', style: 'card' },
        edge: { bg: '#FAF7F2', text: '#0F0F0F', secondary: '#8B9A7D', accent: '#C45A3B', card: '#ffffff', style: 'elegant' },
        stoic: { bg: '#0f0f0f', text: '#e8e4df', secondary: '#6b6561', accent: '#C45A3B', card: 'rgba(255,255,255,0.04)', style: 'stoic' }
    };

    const dimensions = {
        square: { width: 1080, height: 1080 },
        story: { width: 1080, height: 1920 },
        wide: { width: 1200, height: 675 }
    };

    function stripBrandTagline(text) {
        const tagline = String(BRAND_CONFIG.tagline || '').trim();
        if (!tagline) return String(text || '');

        let result = String(text || '');
        const needle = tagline.toLowerCase();

        while (true) {
            const lower = result.toLowerCase();
            const index = lower.indexOf(needle);
            if (index === -1) break;

            let end = index + tagline.length;
            if (result[end] === '.') end += 1;
            result = (result.slice(0, index) + result.slice(end)).trim();
        }

        return result;
    }

    function openImageGenerator() {
        const content = document.getElementById('btn-generate-img').dataset.content;
        imgGenState.content = content;
        document.getElementById('imageGenModal').classList.add('show');
        renderTweetImage();
    }

    function closeImageGenerator() {
        document.getElementById('imageGenModal').classList.remove('show');
    }

    function setTheme(theme) {
        imgGenState.theme = theme;
        document.querySelectorAll('.theme-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.theme-btn[data-theme="${theme}"]`).classList.add('active');
        renderTweetImage();
    }

    function setDimension(dim) {
        imgGenState.dimension = dim;
        document.querySelectorAll('.dim-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.dim-btn[data-dim="${dim}"]`).classList.add('active');
        renderTweetImage();
    }

    async function renderTweetImage() {
        const canvas = document.getElementById('tweetCanvas');
        const ctx = canvas.getContext('2d');
        const dim = dimensions[imgGenState.dimension];
        const theme = themes[imgGenState.theme];
        const profileImg = profileImageLoaded ? await profileImageLoaded : null;
        const style = theme.style;

        canvas.width = dim.width;
        canvas.height = dim.height;

        // Background
        if (theme.bg === 'gradient-brand') {
            const gradient = ctx.createLinearGradient(0, 0, dim.width, dim.height);
            gradient.addColorStop(0, '#1a3a2f');
            gradient.addColorStop(0.5, '#0d1f17');
            gradient.addColorStop(1, '#1a3a2f');
            ctx.fillStyle = gradient;
        } else {
            ctx.fillStyle = theme.bg;
        }
        ctx.fillRect(0, 0, dim.width, dim.height);

        // Add subtle texture/pattern for brand themes
        if (imgGenState.theme === 'brand' || imgGenState.theme === 'bold') {
            ctx.globalAlpha = 0.03;
            for (let i = 0; i < dim.width; i += 60) {
                for (let j = 0; j < dim.height; j += 60) {
                    ctx.fillStyle = '#c9a227';
                    ctx.fillRect(i, j, 1, 1);
                }
            }
            ctx.globalAlpha = 1;
        }

        // Add subtle noise texture for stoic theme
        if (imgGenState.theme === 'stoic') {
            ctx.globalAlpha = 0.015;
            for (let i = 0; i < dim.width; i += 4) {
                for (let j = 0; j < dim.height; j += 4) {
                    if (Math.random() > 0.5) {
                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(i, j, 1, 1);
                    }
                }
            }
            ctx.globalAlpha = 1;
        }

        const padding = dim.width * 0.06;
        const contentWidth = dim.width - (padding * 2);
        const fontSize = style === 'fullbleed' ? 42 : 36;
        const lineHeight = fontSize * 1.5;

        // Strip hashtags and tagline from content (signature added separately)
        const cleanContent = stripBrandTagline(
            imgGenState.content.replace(/#\w+/g, '')
        )
            .replace(/\s+/g, ' ')
            .trim();

        // Calculate content height first
        ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
        const lines = wrapText(ctx, cleanContent, contentWidth - 40);
        const textHeight = lines.length * lineHeight;

        if (style === 'fullbleed') {
            // BOLD STYLE: No card, large centered text
            const avatarSize = 100;
            const startY = (dim.height - (avatarSize + 40 + textHeight + 80)) / 2;

            // Avatar centered
            const avatarX = (dim.width - avatarSize) / 2;
            const avatarY = startY;
            drawAvatar(ctx, profileImg, avatarX, avatarY, avatarSize, theme);

            // Name below avatar
            ctx.textAlign = 'center';
            ctx.fillStyle = theme.text;
            ctx.font = 'bold 28px Outfit, sans-serif';
            ctx.fillText(PROFILE.name || BRAND_CONFIG.brand_name, dim.width / 2, avatarY + avatarSize + 35);

            // Quote text - large and centered
            ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
            let y = avatarY + avatarSize + 90;
            lines.forEach(line => {
                drawTextWithHashtags(ctx, line, dim.width / 2, y, theme, 'center');
                y += lineHeight;
            });

            // Bottom branding
            ctx.fillStyle = theme.accent;
            ctx.font = '600 20px JetBrains Mono, monospace';
            ctx.fillText(BRAND_CONFIG.domain || '', dim.width / 2, dim.height - padding);

        } else if (style === 'centered') {
            // MINIMAL STYLE: Clean, centered, subtle card
            const cardPadding = 50;
            const cardHeight = textHeight + 200;
            const cardY = (dim.height - cardHeight) / 2;

            // Subtle shadow card
            ctx.shadowColor = 'rgba(0,0,0,0.08)';
            ctx.shadowBlur = 60;
            ctx.shadowOffsetY = 20;
            ctx.fillStyle = theme.card;
            roundRect(ctx, padding, cardY, contentWidth, cardHeight, 20);
            ctx.fill();
            ctx.shadowColor = 'transparent';

            // Quote text - centered in card
            ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
            ctx.textAlign = 'center';
            let y = cardY + cardPadding + 30;
            lines.forEach(line => {
                drawTextWithHashtags(ctx, line, dim.width / 2, y, theme, 'center');
                y += lineHeight;
            });

            // Profile at bottom of card
            const avatarSize = 60;
            const profileY = cardY + cardHeight - 80;
            const avatarX = dim.width / 2 - 100;
            drawAvatar(ctx, profileImg, avatarX, profileY, avatarSize, theme);

            ctx.textAlign = 'left';
            ctx.fillStyle = theme.text;
            ctx.font = 'bold 22px Outfit, sans-serif';
            ctx.fillText(PROFILE.name || BRAND_CONFIG.brand_name, avatarX + avatarSize + 15, profileY + 25);
            ctx.fillStyle = theme.secondary;
            ctx.font = '18px Outfit, sans-serif';
            ctx.fillText(PROFILE.handle || ('@' + BRAND_CONFIG.handle), avatarX + avatarSize + 15, profileY + 48);

        } else if (style === 'elegant') {
            // EDGE THEME: Clean, sophisticated with warm accents
            const cardPadding = 60;
            const signatureHeight = 80;
            const totalContentHeight = textHeight + signatureHeight + cardPadding * 2;
            const cardY = (dim.height - totalContentHeight) / 2;

            // Subtle card with soft shadow
            ctx.shadowColor = 'rgba(0,0,0,0.08)';
            ctx.shadowBlur = 40;
            ctx.shadowOffsetY = 10;
            ctx.fillStyle = theme.card;
            roundRect(ctx, padding + 20, cardY, contentWidth - 40, totalContentHeight, 16);
            ctx.fill();
            ctx.shadowColor = 'transparent';

            // Left accent bar
            ctx.fillStyle = theme.accent;
            roundRect(ctx, padding + 20, cardY + 30, 4, totalContentHeight - 60, 2);
            ctx.fill();

            // Quote text - centered
            ctx.font = `500 ${fontSize}px Georgia, serif`;
            ctx.textAlign = 'center';
            let y = cardY + cardPadding + 20;
            lines.forEach(line => {
                ctx.fillStyle = theme.text;
                ctx.fillText(line, dim.width / 2, y);
                y += lineHeight;
            });

            // Signature - italic
            ctx.fillStyle = theme.accent;
            ctx.font = 'italic 500 24px Georgia, serif';
            ctx.fillText(BRAND_CONFIG.tagline || '', dim.width / 2, y + 40);

            // Branding at bottom
            ctx.fillStyle = theme.secondary;
            ctx.font = '500 16px Georgia, serif';
            ctx.fillText(BRAND_CONFIG.domain || '', dim.width / 2, dim.height - padding);

        } else if (style === 'stoic') {
            // STOIC THEME: Classical, dignified with terracotta accents
            const centerX = dim.width / 2;
            const totalContentHeight = textHeight + 200;
            const startY = (dim.height - totalContentHeight) / 2;

            // Top decorative line
            ctx.strokeStyle = theme.secondary;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(centerX - 200, startY);
            ctx.lineTo(centerX + 200, startY);
            ctx.stroke();

            // Quote text - elegant serif centered
            ctx.font = `italic 400 ${fontSize}px Georgia, serif`;
            ctx.textAlign = 'center';
            ctx.fillStyle = theme.text;
            let y = startY + 60;

            // Opening quote mark
            ctx.font = 'italic 72px Georgia, serif';
            ctx.fillStyle = theme.accent;
            ctx.fillText('"', centerX - ctx.measureText(lines[0] || '').width / 2 - 30, y - 10);

            // Quote lines
            ctx.font = `italic 400 ${fontSize}px Georgia, serif`;
            ctx.fillStyle = theme.text;
            lines.forEach(line => {
                ctx.fillText(line, centerX, y);
                y += lineHeight;
            });

            // Bottom decorative line
            ctx.strokeStyle = theme.secondary;
            ctx.beginPath();
            ctx.moveTo(centerX - 200, y + 30);
            ctx.lineTo(centerX + 200, y + 30);
            ctx.stroke();

            // Signature line
            ctx.fillStyle = theme.accent;
            ctx.font = 'italic 600 28px Georgia, serif';
            ctx.fillText(BRAND_CONFIG.tagline || '', centerX, y + 80);

            // Branding at bottom
            ctx.fillStyle = theme.secondary;
            ctx.font = '400 14px Georgia, serif';
            ctx.fillText(BRAND_CONFIG.domain || '', centerX, dim.height - padding);

        } else {
            // CARD STYLE (brand, dark): Quote in centered card
            const cardPadding = 50;
            const signatureHeight = 60;
            const cardHeight = textHeight + signatureHeight + cardPadding * 2;
            const cardY = (dim.height - cardHeight) / 2;

            // Card with glassmorphism effect
            ctx.shadowColor = 'rgba(0,0,0,0.3)';
            ctx.shadowBlur = 50;
            ctx.shadowOffsetY = 15;
            ctx.fillStyle = theme.card;
            roundRect(ctx, padding, cardY, contentWidth, cardHeight, 24);
            ctx.fill();
            ctx.shadowColor = 'transparent';

            // Gold accent line at top
            ctx.fillStyle = theme.accent;
            ctx.beginPath();
            ctx.roundRect(padding, cardY, contentWidth, 4, [24, 24, 0, 0]);
            ctx.fill();

            // Quote text - centered in card
            ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
            ctx.textAlign = 'center';
            let y = cardY + cardPadding + 30;
            lines.forEach(line => {
                drawTextWithHashtags(ctx, line, dim.width / 2, y, theme, 'center');
                y += lineHeight;
            });

            // Signature line - centered below quote
            ctx.textAlign = 'center';
            ctx.fillStyle = theme.accent;
            ctx.font = 'italic 600 26px Outfit, sans-serif';
            ctx.fillText(BRAND_CONFIG.tagline || '', dim.width / 2, y + 25);

            // Branding at bottom of image
            ctx.fillStyle = theme.accent;
            ctx.font = '600 24px JetBrains Mono, monospace';
            ctx.textAlign = 'center';
            ctx.fillText(BRAND_CONFIG.domain || '', dim.width / 2, dim.height - padding);
        }
    }

    function drawAvatar(ctx, profileImg, x, y, size, theme) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(x + size/2, y + size/2, size/2, 0, Math.PI * 2);
        ctx.closePath();
        ctx.clip();
        if (profileImg) {
            ctx.drawImage(profileImg, x, y, size, size);
        } else {
            const grad = ctx.createLinearGradient(x, y, x + size, y + size);
            grad.addColorStop(0, '#1a3a2f');
            grad.addColorStop(1, '#0d1f17');
            ctx.fillStyle = grad;
            ctx.fillRect(x, y, size, size);
            ctx.fillStyle = '#c9a227';
            ctx.font = `bold ${size * 0.5}px Outfit, sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText((PROFILE.name || BRAND_CONFIG.brand_name)[0], x + size/2, y + size/2);
        }
        ctx.restore();
    }

    function drawTextWithHashtags(ctx, line, x, y, theme, align) {
        const parts = line.split(/(#\\w+)/g);
        if (align === 'center') {
            const totalWidth = parts.reduce((w, p) => w + ctx.measureText(p).width, 0);
            x = x - totalWidth / 2;
        }
        parts.forEach(part => {
            ctx.fillStyle = part.startsWith('#') ? theme.accent : theme.text;
            ctx.textAlign = 'left';
            ctx.fillText(part, x, y);
            x += ctx.measureText(part).width;
        });
    }

    function roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    function wrapText(ctx, text, maxWidth) {
        const words = text.split(' ');
        const lines = [];
        let currentLine = '';

        words.forEach(word => {
            // Handle newlines
            if (word.includes('\\n')) {
                const parts = word.split('\\n');
                parts.forEach((part, i) => {
                    if (i > 0) {
                        lines.push(currentLine.trim());
                        currentLine = '';
                    }
                    const testLine = currentLine + part + ' ';
                    if (ctx.measureText(testLine).width > maxWidth && currentLine !== '') {
                        lines.push(currentLine.trim());
                        currentLine = part + ' ';
                    } else {
                        currentLine = testLine;
                    }
                });
            } else {
                const testLine = currentLine + word + ' ';
                if (ctx.measureText(testLine).width > maxWidth && currentLine !== '') {
                    lines.push(currentLine.trim());
                    currentLine = word + ' ';
                } else {
                    currentLine = testLine;
                }
            }
        });
        if (currentLine.trim()) lines.push(currentLine.trim());
        return lines;
    }

    function downloadImage() {
        const canvas = document.getElementById('tweetCanvas');
        const link = document.createElement('a');
        link.download = 'social-kanban-' + Date.now() + '.png';
        link.href = canvas.toDataURL('image/png');
        link.click();
        showToast('Image downloaded!');
    }

    function openInstagram() {
        downloadImage();
        setTimeout(() => {
            window.open('https://www.instagram.com/', '_blank');
            showToast('Image downloaded. Upload it to Instagram.');
        }, 500);
    }

    async function openLinkedIn() {
        const btn = document.querySelector('.btn-share.linkedin');
        btn.disabled = true;
        btn.textContent = 'Generating...';

        try {
            const canvas = await generateTweetCanvas(imgGenState.content);
            const imageData = canvas.toDataURL('image/png');

            btn.textContent = 'Uploading...';

            const uploadResp = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({image: imageData})
            });

            const uploadData = await uploadResp.json();
            if (!uploadResp.ok || !uploadData.secure_url) {
                throw new Error(uploadData.error || 'Failed to upload image');
            }

            btn.textContent = 'Posting...';

            const resp = await fetch('/api/post/linkedin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    content: imgGenState.content,
                    image_url: uploadData.secure_url
                })
            });

            const data = await resp.json();
            if (!resp.ok || !data.success) {
                throw new Error(data.error || 'Failed to post');
            }

            btn.textContent = '✓ Posted!';
            btn.style.background = '#22c55e';
            btn.style.color = '#fff';
            showToast('Posted to LinkedIn with image!');

            setTimeout(() => {
                btn.textContent = '💼 LinkedIn';
                btn.style.background = '';
                btn.style.color = '';
                btn.disabled = false;
            }, 3000);
        } catch (err) {
            btn.textContent = '💼 LinkedIn';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to LinkedIn', true);
        }
    }

    async function postToFacebook() {
        const btn = document.querySelector('.btn-share.facebook');
        btn.disabled = true;
        btn.textContent = 'Generating...';

        try {
            const canvas = await generateTweetCanvas(imgGenState.content);
            const imageData = canvas.toDataURL('image/png');

            btn.textContent = 'Uploading...';

            const uploadResp = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({image: imageData})
            });

            const uploadData = await uploadResp.json();
            if (!uploadResp.ok || !uploadData.secure_url) {
                throw new Error(uploadData.error || 'Failed to upload image');
            }

            btn.textContent = 'Posting...';

            const resp = await fetch('/api/post/facebook', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    content: imgGenState.content,
                    image_url: uploadData.secure_url
                })
            });

            const data = await resp.json();

            if (resp.ok && data.success) {
                btn.textContent = '✓ Posted!';
                btn.style.background = '#22c55e';
                btn.style.color = '#fff';
                showToast('Posted to Facebook with image!');

                setTimeout(() => {
                    btn.textContent = '📘 Facebook';
                    btn.style.background = '';
                    btn.style.color = '';
                    btn.disabled = false;
                }, 3000);
            } else {
                throw new Error(data.error || 'Failed to post');
            }
        } catch (err) {
            btn.textContent = '📘 Facebook';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to Facebook', true);
        }
    }

    // Generate tweet image for a given content (returns canvas) - uses Brand theme
    async function generateTweetCanvas(content) {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        const dim = { width: 1080, height: 1080 };
        const theme = { bg: '#1a3a2f', text: '#f5f0e6', secondary: '#a8b5a0', accent: '#c9a227', card: 'rgba(255,255,255,0.08)' };
        const profileImg = profileImageLoaded ? await profileImageLoaded : null;

        canvas.width = dim.width;
        canvas.height = dim.height;

        // Brand green background
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, dim.width, dim.height);

        // Subtle texture
        ctx.globalAlpha = 0.03;
        for (let i = 0; i < dim.width; i += 60) {
            for (let j = 0; j < dim.height; j += 60) {
                ctx.fillStyle = '#c9a227';
                ctx.fillRect(i, j, 1, 1);
            }
        }
        ctx.globalAlpha = 1;

        const padding = dim.width * 0.06;
        const contentWidth = dim.width - (padding * 2);
        const fontSize = 36;
        const lineHeight = fontSize * 1.5;

        // Strip hashtags and tagline from content (signature added separately)
        const cleanContent = stripBrandTagline(
            content.replace(/#\w+/g, '')
        )
            .replace(/\s+/g, ' ')
            .trim();

        // Calculate content height
        ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
        const lines = wrapText(ctx, cleanContent, contentWidth - 80);
        const textHeight = lines.length * lineHeight;

        // Dynamic card - quote-only, positioned higher for link preview
        const cardPadding = 50;
        const signatureHeight = 50;
        const cardHeight = textHeight + signatureHeight + cardPadding * 2 + 20;
        const cardY = (dim.height - cardHeight) / 2 - 60;

        // Card with glassmorphism
        ctx.shadowColor = 'rgba(0,0,0,0.3)';
        ctx.shadowBlur = 50;
        ctx.shadowOffsetY = 15;
        ctx.fillStyle = theme.card;
        roundRect(ctx, padding, cardY, contentWidth, cardHeight, 24);
        ctx.fill();
        ctx.shadowColor = 'transparent';

        // Gold accent line
        ctx.fillStyle = theme.accent;
        ctx.beginPath();
        ctx.roundRect(padding, cardY, contentWidth, 4, [24, 24, 0, 0]);
        ctx.fill();

        // Quote text - centered
        ctx.font = `600 ${fontSize}px Outfit, sans-serif`;
        ctx.textAlign = 'center';
        let y = cardY + cardPadding + 40;
        lines.forEach(line => {
            ctx.fillStyle = theme.text;
            ctx.fillText(line, dim.width / 2, y);
            y += lineHeight;
        });

        // Signature line - centered
        ctx.fillStyle = theme.accent;
        ctx.font = 'italic 600 26px Outfit, sans-serif';
        ctx.fillText(BRAND_CONFIG.tagline || '', dim.width / 2, y + 30);

        // Branding
        ctx.fillStyle = theme.accent;
        ctx.font = '600 18px JetBrains Mono, monospace';
        ctx.textAlign = 'center';
        ctx.fillText(BRAND_CONFIG.domain || '', dim.width / 2, dim.height - padding);

        return canvas;
    }

    function markCardAsPosted(postId) {
        if (!postId) {
            syncBoardStats();
            return;
        }
        const card = document.querySelector(`.card[data-id="${postId}"]`);
        const postedColumn = document.querySelector('.col-posted .column-body');
        if (!card || !postedColumn) {
            syncBoardStats();
            return;
        }

        const postedEmptyState = postedColumn.querySelector('.empty-state');
        if (postedEmptyState) postedEmptyState.remove();

        const nowLabel = new Date().toLocaleString('en-US', {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
        });
        card.dataset.postedLabel = nowLabel;
        card.classList.remove('pending', 'approved');
        card.classList.add('posted');
        postedColumn.prepend(card);
        updateCardPresentation(card, 'posted');
        syncBoardStats();
    }

    async function postToTwitterFromModal() {
        const btn = document.getElementById('btn-post-x');
        const postId = btn.dataset.postId;

        if (!postId) {
            showToast('Open a scheduled X draft first', true);
            return;
        }

        btn.disabled = true;
        btn.textContent = 'Posting...';

        try {
            const resp = await fetch('/api/post/tweet', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({post_id: postId})
            });
            const data = await resp.json();

            if (!resp.ok || !data.success) {
                throw new Error(data.error || 'Failed to post to X');
            }

            btn.textContent = '✓ Posted!';
            btn.style.background = '#22c55e';
            markCardAsPosted(postId);
            showToast('Posted to X!');

            setTimeout(() => {
                btn.textContent = 'Post to 𝕏';
                btn.style.background = '';
                btn.disabled = false;
            }, 3000);
        } catch (err) {
            btn.textContent = 'Post to 𝕏';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to X', true);
        }
    }

    // Post to Instagram from modal (uses existing image or generates new one)
    async function postToInstagramFromModal() {
        const btn = document.getElementById('btn-post-instagram');
        const content = btn.dataset.content;
        const postId = btn.dataset.postId;
        const existingImageUrl = btn.dataset.imageUrl;

        if (!content) {
            showToast('No content to post', true);
            return;
        }

        btn.disabled = true;

        try {
            let imageUrl;

            // Use existing image URL if available (stoic cards), otherwise generate
            if (existingImageUrl) {
                btn.textContent = 'Posting...';
                imageUrl = existingImageUrl;
            } else {
                btn.textContent = 'Generating...';
                const canvas = await generateTweetCanvas(content);
                const imageData = canvas.toDataURL('image/png');

                btn.textContent = 'Uploading...';

                const uploadResp = await fetch('/api/cloudinary/upload', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({image: imageData})
                });
                const uploadData = await uploadResp.json();

                if (!uploadResp.ok || !uploadData.secure_url) {
                    throw new Error(uploadData.error || 'Failed to upload image');
                }

                btn.textContent = 'Posting...';
                imageUrl = uploadData.secure_url;
            }

            // Post to Instagram
            const postResp = await fetch('/api/post/instagram', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    post_id: postId || null,
                    image_url: imageUrl,
                    caption: content
                })
            });
            const postData = await postResp.json();

            if (!postResp.ok || !postData.success) {
                throw new Error(postData.error || 'Failed to post to Instagram');
            }

            btn.textContent = '✓ Posted!';
            btn.style.background = '#22c55e';
            markCardAsPosted(postId);
            showToast('Posted to Instagram!');

            setTimeout(() => {
                btn.textContent = '📷 Instagram';
                btn.style.background = '';
                btn.disabled = false;
            }, 3000);

        } catch (err) {
            btn.textContent = '📷 Instagram';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to Instagram', true);
        }
    }

    // Post to Facebook from modal (uses existing image or generates new one)
    async function postToFacebookFromModal() {
        const btn = document.getElementById('btn-post-facebook');
        const content = btn.dataset.content;
        const postId = btn.dataset.postId;
        const existingImageUrl = btn.dataset.imageUrl;

        if (!content) {
            showToast('No content to post', true);
            return;
        }

        btn.disabled = true;

        try {
            let imageUrl;

            // Use existing image URL if available (stoic cards), otherwise generate
            if (existingImageUrl) {
                btn.textContent = 'Posting...';
                imageUrl = existingImageUrl;
            } else {
                btn.textContent = 'Generating...';
                const canvas = await generateTweetCanvas(content);
                const imageData = canvas.toDataURL('image/png');

                btn.textContent = 'Uploading...';

                const uploadResp = await fetch('/api/cloudinary/upload', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({image: imageData})
                });

                const uploadData = await uploadResp.json();
                if (!uploadResp.ok || !uploadData.secure_url) {
                    throw new Error(uploadData.error || 'Failed to upload image');
                }

                btn.textContent = 'Posting...';
                imageUrl = uploadData.secure_url;
            }

            const resp = await fetch('/api/post/facebook', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    post_id: postId || null,
                    content: content,
                    image_url: imageUrl
                })
            });

            const data = await resp.json();

            if (resp.ok && data.success) {
                btn.textContent = '✓ Posted!';
                btn.style.background = '#22c55e';
                markCardAsPosted(postId);
                showToast('Posted to Facebook with image!');

                setTimeout(() => {
                    btn.textContent = '📘 Facebook';
                    btn.style.background = '';
                    btn.disabled = false;
                }, 3000);
            } else {
                throw new Error(data.error || 'Failed to post');
            }
        } catch (err) {
            btn.textContent = '📘 Facebook';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to Facebook', true);
        }
    }

    async function postToLinkedInFromModal() {
        const btn = document.getElementById('btn-post-linkedin');
        const content = btn.dataset.content;
        const postId = btn.dataset.postId;
        const existingImageUrl = btn.dataset.imageUrl;

        if (!content) {
            showToast('No content to post', true);
            return;
        }

        btn.disabled = true;

        try {
            let imageUrl = existingImageUrl || '';

            if (!imageUrl) {
                btn.textContent = 'Generating...';
                const canvas = await generateTweetCanvas(content);
                const imageData = canvas.toDataURL('image/png');

                btn.textContent = 'Uploading...';

                const uploadResp = await fetch('/api/cloudinary/upload', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({image: imageData})
                });
                const uploadData = await uploadResp.json();

                if (!uploadResp.ok || !uploadData.secure_url) {
                    throw new Error(uploadData.error || 'Failed to upload image');
                }

                imageUrl = uploadData.secure_url;
            }

            btn.textContent = 'Posting...';

            const resp = await fetch('/api/post/linkedin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    post_id: postId || null,
                    content: content,
                    image_url: imageUrl
                })
            });

            const data = await resp.json();

            if (!resp.ok || !data.success) {
                throw new Error(data.error || 'Failed to post');
            }

            btn.textContent = '✓ Posted!';
            btn.style.background = '#22c55e';
            markCardAsPosted(postId);
            showToast('Posted to LinkedIn!');

            setTimeout(() => {
                btn.textContent = '💼 LinkedIn';
                btn.style.background = '';
                btn.disabled = false;
            }, 3000);
        } catch (err) {
            btn.textContent = '💼 LinkedIn';
            btn.disabled = false;
            showToast(err.message || 'Failed to post to LinkedIn', true);
        }
    }

    function showToast(msg, isError) {
        const t = document.getElementById('toast');
        t.textContent = msg;
        t.className = 'toast show' + (isError ? ' error' : '');
        setTimeout(() => t.className = 'toast', 2500);
    }

    // Close image generator on backdrop click
    document.getElementById('imageGenModal').addEventListener('click', e => {
        if (e.target.id === 'imageGenModal') closeImageGenerator();
    });

    // Escape key for image generator
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && document.getElementById('imageGenModal').classList.contains('show')) {
            closeImageGenerator();
        }
    });

    document.getElementById('draftModal').addEventListener('click', e => {
        if (e.target.id === 'draftModal') closeDraftModal();
    });

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && document.getElementById('draftModal').classList.contains('show')) {
            closeDraftModal();
        }
    });


    let draftImageFile = null;
    let draftEditPostId = null;
    let draftEditPlatform = null;

    function resetDraftForm() {
        draftImageFile = null;
        draftEditPostId = null;
        draftEditPlatform = null;
        document.getElementById('draftContent').value = '';
        document.getElementById('draftSchedule').value = '';
        document.getElementById('draftImageUrl').value = '';
        document.getElementById('draftAttachmentName').textContent = '';
        document.getElementById('draftAttachmentPreview').classList.remove('show');
        document.getElementById('draftImageFile').value = '';
        document.querySelectorAll('input[name="draftPlatform"]').forEach((input, index) => {
            input.disabled = false;
            input.checked = index === 0;
        });
    }

    function openDraftModal() {
        document.getElementById('draftModal').classList.add('show');
        resetDraftForm();
        document.getElementById('draftSubmitBtn').textContent = 'Create Draft';
    }

    function closeDraftModal() {
        document.getElementById('draftModal').classList.remove('show');
    }

    function openEditDraftFromModal() {
        const editBtn = document.getElementById('btn-edit-draft');
        const postId = editBtn.dataset.postId;
        if (!postId) {
            window.showToast('Open a draft first', true);
            return;
        }

        closeModal();
        openDraftModal();
        draftEditPostId = postId;
        draftEditPlatform = editBtn.dataset.platform || 'twitter';
        document.getElementById('draftContent').value = editBtn.dataset.content || '';
        document.getElementById('draftSchedule').value = toDateTimeLocalValue(editBtn.dataset.scheduledAt || '');
        document.getElementById('draftImageUrl').value = editBtn.dataset.imageUrl || '';
        document.querySelectorAll('input[name="draftPlatform"]').forEach((input) => {
            input.checked = input.value === draftEditPlatform;
            input.disabled = true;
        });
        document.getElementById('draftSubmitBtn').textContent = 'Save Changes';
    }

    async function deleteDraftFromModal() {
        const deleteBtn = document.getElementById('btn-delete-draft');
        const postId = deleteBtn.dataset.postId;
        if (!postId) {
            window.showToast('Open a draft first', true);
            return;
        }
        if (!window.confirm('Remove this draft?')) return;

        deleteBtn.disabled = true;
        try {
            const resp = await fetch(`/api/post/${postId}`, {
                method: 'DELETE',
            });
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                throw new Error(data.error || 'Failed to remove draft');
            }
            const card = document.querySelector(`.card[data-id="${postId}"]`);
            if (card) card.remove();
            window.syncBoardStats();
            closeModal();
            window.showToast('Draft removed');
        } catch (err) {
            window.showToast(err.message || 'Failed to remove draft', true);
        } finally {
            deleteBtn.disabled = false;
        }
    }

    function selectDraftImage() {
        document.getElementById('draftImageFile').click();
    }

    function handleDraftImageFile(event) {
        const file = event.target.files?.[0];
        if (!file) return;
        if (!file.type.startsWith('image/')) {
            showToast('Select an image file', true);
            event.target.value = '';
            return;
        }
        draftImageFile = file;
        document.getElementById('draftAttachmentName').textContent = file.name;
        document.getElementById('draftAttachmentPreview').classList.add('show');
    }

    function removeDraftImage() {
        draftImageFile = null;
        document.getElementById('draftImageFile').value = '';
        document.getElementById('draftAttachmentName').textContent = '';
        document.getElementById('draftAttachmentPreview').classList.remove('show');
    }

    function fileToDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(new Error('Failed to read image file'));
            reader.readAsDataURL(file);
        });
    }

    async function uploadDraftImageIfNeeded() {
        const imageUrl = document.getElementById('draftImageUrl').value.trim();
        if (draftImageFile) {
            const imageData = await fileToDataUrl(draftImageFile);
            const uploadResp = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({image: imageData})
            });
            const uploadData = await uploadResp.json();
            if (!uploadResp.ok || !uploadData.secure_url) {
                throw new Error(uploadData.error || 'Failed to upload image');
            }
            return uploadData.secure_url;
        }
        return imageUrl || '';
    }

    async function submitDraft() {
        const btn = document.getElementById('draftSubmitBtn');
        const content = document.getElementById('draftContent').value.trim();
        const selectedPlatforms = Array.from(document.querySelectorAll('input[name="draftPlatform"]:checked')).map((input) => input.value);
        const scheduledValue = document.getElementById('draftSchedule').value;

        if (!content) {
            showToast('Add some post content first', true);
            return;
        }
        if (!selectedPlatforms.length) {
            showToast('Select at least one platform', true);
            return;
        }

        btn.disabled = true;
        btn.textContent = draftImageFile ? 'Uploading image...' : (draftEditPostId ? 'Saving...' : 'Creating...');

        try {
            const mediaUrl = await uploadDraftImageIfNeeded();
            if (draftImageFile) {
                btn.textContent = draftEditPostId ? 'Saving...' : 'Creating...';
            }

            const scheduledAt = scheduledValue ? new Date(scheduledValue).toISOString() : null;
            if (draftEditPostId) {
                const resp = await fetch(`/api/post/${draftEditPostId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        content,
                        scheduled_at: scheduledAt,
                        media_url: mediaUrl || null,
                    })
                });
                const data = await resp.json();
                if (!resp.ok || !data.success || !data.post) {
                    throw new Error(data.error || 'Failed to update draft');
                }
                const card = document.querySelector(`.card[data-id="${draftEditPostId}"]`);
                if (card) applyPostCardData(card, data.post);
                window.syncBoardStats();
                closeDraftModal();
                window.showToast('Draft updated');
            } else {
                const resp = await fetch('/api/posts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        content,
                        platform: selectedPlatforms,
                        scheduled_at: scheduledAt,
                        media_url: mediaUrl || null,
                        status: 'pending',
                    })
                });
                const data = await resp.json();

                if (!resp.ok || !data.success) {
                    throw new Error(data.error || 'Failed to create draft');
                }

                const pendingColumn = document.querySelector('.col-pending .column-body');
                if (pendingColumn) {
                    [...data.posts].reverse().forEach((post) => {
                        pendingColumn.prepend(window.createPostCard(post));
                    });
                }

                const emptyState = pendingColumn?.querySelector('.empty-state');
                if (emptyState) emptyState.remove();
                const scheduledEmptyState = document.querySelector('.col-approved .empty-state');
                const postedEmptyState = document.querySelector('.col-posted .empty-state');
                if (scheduledEmptyState && document.querySelector('.col-approved .card')) scheduledEmptyState.remove();
                if (postedEmptyState && document.querySelector('.col-posted .card')) postedEmptyState.remove();

                window.syncBoardStats();
                closeDraftModal();
                window.showToast(`${data.posts.length} draft${data.posts.length === 1 ? '' : 's'} created`);
            }
        } catch (err) {
            window.showToast(err.message || 'Failed to create draft', true);
        } finally {
            document.querySelectorAll('input[name="draftPlatform"]').forEach((input) => {
                input.disabled = false;
            });
            btn.disabled = false;
            btn.textContent = 'Create Draft';
        }
    }

    // Upload Modal Functions (outside IIFE to be globally accessible)
    let selectedFile = null;

    function openUploadModal() {
        document.getElementById('uploadModal').classList.add('show');
        resetUploadForm();
    }

    function closeUploadModal() {
        document.getElementById('uploadModal').classList.remove('show');
        resetUploadForm();
    }

    // Stoic Modal Functions
    let stoicCardData = null;
    let stoicImageData = null;

    function openStoicModal() {
        document.getElementById('stoicModal').classList.add('show');
        loadStoicEntry();
        // Reset state
        stoicCardData = null;
        stoicImageData = null;
        document.getElementById('stoicContent').innerHTML = `
            <div class="stoic-loading" id="stoicInitial">
                <p style="color: var(--text-secondary); margin-bottom: 1rem;">Generate today's Stoic card</p>
            </div>
        `;
        document.getElementById('stoicActions').innerHTML = `
            <button class="stoic-btn stoic-btn-cancel" onclick="closeStoicModal()">Cancel</button>
            <button class="stoic-btn stoic-btn-generate" id="btnStoicGenerate" onclick="generateStoicCard()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
                </svg>
                Generate Card
            </button>
        `;
    }

    function closeStoicModal() {
        document.getElementById('stoicModal').classList.remove('show');
        stoicCardData = null;
        stoicImageData = null;
    }

    async function loadStoicEntry() {
        const today = new Date();
        const dateStr = today.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
        document.getElementById('stoicDate').textContent = dateStr.toUpperCase();

        try {
            const response = await fetch('/api/stoic/entry');
            const data = await response.json();

            if (data.error) {
                document.getElementById('stoicEntryTitle').textContent = 'Error loading entry';
                return;
            }

            document.getElementById('stoicEntryTitle').textContent = data.title;
            document.getElementById('stoicAuthor').textContent = data.author;
        } catch (err) {
            document.getElementById('stoicEntryTitle').textContent = 'Error loading entry';
        }
    }

    // Render stoic card to canvas (client-side) - matches original aesthetic
    async function renderStoicCanvas(data) {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        canvas.width = 1080;
        canvas.height = 1350;

        // Colors matching original
        const bgColor = '#0a0a0a';
        const cardBg = '#111111';
        const accentColor = '#C45A3B';
        const textColor = '#e8e8e8';
        const mutedColor = '#7a7a7a';
        const dimColor = '#505050';
        const borderColor = '#1a1a1a';

        // Background
        ctx.fillStyle = bgColor;
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        // Card background (centered, larger)
        const cardX = 90;
        const cardY = 120;
        const cardW = 900;
        const cardH = 1110;

        // Card with subtle gradient
        const cardGrad = ctx.createLinearGradient(cardX, cardY, cardX, cardY + cardH);
        cardGrad.addColorStop(0, '#151515');
        cardGrad.addColorStop(1, '#0d0d0d');
        ctx.fillStyle = cardGrad;
        ctx.beginPath();
        ctx.roundRect(cardX, cardY, cardW, cardH, 24);
        ctx.fill();

        // Card border
        ctx.strokeStyle = borderColor;
        ctx.lineWidth = 1;
        ctx.stroke();

        const centerX = canvas.width / 2;
        let y = cardY + 90;

        // Date - with letter spacing simulation
        ctx.fillStyle = dimColor;
        ctx.font = '14px Georgia';
        ctx.textAlign = 'center';
        const dateText = data.date.toUpperCase();
        ctx.fillText(addLetterSpacing(dateText, 3), centerX, y);
        y += 50;

        // Philosopher - prominent with letter spacing
        ctx.fillStyle = accentColor;
        ctx.font = '600 18px Georgia';
        const authorText = data.author.toUpperCase();
        ctx.fillText(addLetterSpacing(authorText, 4), centerX, y);
        y += 45;

        // Title - large italic
        ctx.fillStyle = textColor;
        ctx.font = 'italic 42px Georgia';
        ctx.fillText(data.title, centerX, y);
        y += 70;

        // Divider - elegant gradient line
        const gradient = ctx.createLinearGradient(centerX - 250, y, centerX + 250, y);
        gradient.addColorStop(0, 'transparent');
        gradient.addColorStop(0.2, '#333333');
        gradient.addColorStop(0.5, '#444444');
        gradient.addColorStop(0.8, '#333333');
        gradient.addColorStop(1, 'transparent');
        ctx.strokeStyle = gradient;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(centerX - 250, y);
        ctx.lineTo(centerX + 250, y);
        ctx.stroke();
        y += 65;

        // Three points with better spacing
        const points = [
            { title: data.point1_title, meaning: data.point1_meaning, trading: data.point1_trading },
            { title: data.point2_title, meaning: data.point2_meaning, trading: data.point2_trading },
            { title: data.point3_title, meaning: data.point3_meaning, trading: data.point3_trading }
        ];

        points.forEach((point, i) => {
            // Point title - orange, bold
            ctx.fillStyle = accentColor;
            ctx.font = 'bold 22px Georgia';
            ctx.fillText((i + 1) + '. ' + point.title, centerX, y);
            y += 35;

            // Meaning - italic, muted
            ctx.fillStyle = mutedColor;
            ctx.font = 'italic 18px Georgia';
            ctx.fillText(point.meaning, centerX, y);
            y += 32;

            // Trading application - white, clear
            ctx.fillStyle = textColor;
            ctx.font = '20px Georgia';
            ctx.fillText(point.trading, centerX, y);
            y += 70;
        });

        // Bottom section divider
        y += 5;
        const bottomGrad = ctx.createLinearGradient(centerX - 280, y, centerX + 280, y);
        bottomGrad.addColorStop(0, 'transparent');
        bottomGrad.addColorStop(0.15, '#222222');
        bottomGrad.addColorStop(0.5, '#2a2a2a');
        bottomGrad.addColorStop(0.85, '#222222');
        bottomGrad.addColorStop(1, 'transparent');
        ctx.strokeStyle = bottomGrad;
        ctx.beginPath();
        ctx.moveTo(centerX - 280, y);
        ctx.lineTo(centerX + 280, y);
        ctx.stroke();
        y += 50;

        // Closing wisdom - italic
        ctx.fillStyle = mutedColor;
        ctx.font = 'italic 20px Georgia';
        const wisdomLines = wrapText(ctx, data.closing_wisdom, cardW - 180);
        wisdomLines.forEach(line => {
            ctx.fillText(line, centerX, y);
            y += 32;
        });
        y += 25;

        // Key takeaway - bold orange
        ctx.fillStyle = accentColor;
        ctx.font = 'bold 22px Georgia';
        ctx.fillText(data.key_takeaway, centerX, y);
        y += 70;

        // CTA - brand tagline
        ctx.fillStyle = '#666666';
        ctx.font = 'italic 17px Georgia';
        ctx.fillText(BRAND_CONFIG.tagline || '', centerX, y);
        y += 35;

        // Domain branding - bold with letter spacing
        ctx.fillStyle = textColor;
        ctx.font = 'bold 16px Georgia';
        ctx.fillText(addLetterSpacing(BRAND_CONFIG.domain || '', 2), centerX, y);

        return canvas;
    }

    // Helper to simulate letter-spacing
    function addLetterSpacing(text, spacing) {
        return text.split('').join(String.fromCharCode(8202).repeat(spacing));
    }

    function wrapText(ctx, text, maxWidth) {
        const words = text.split(' ');
        const lines = [];
        let line = '';

        words.forEach(word => {
            const testLine = line + word + ' ';
            if (ctx.measureText(testLine).width > maxWidth && line) {
                lines.push(line.trim());
                line = word + ' ';
            } else {
                line = testLine;
            }
        });
        if (line) lines.push(line.trim());
        return lines;
    }

    async function generateStoicCard() {
        const btn = document.getElementById('btnStoicGenerate');
        const content = document.getElementById('stoicContent');

        btn.disabled = true;
        btn.innerHTML = '<div class="stoic-spinner" style="width:16px;height:16px;border-width:2px;margin:0;"></div> Generating...';

        content.innerHTML = `
            <div class="stoic-loading">
                <div class="stoic-spinner"></div>
                <div class="stoic-loading-text">Generating wisdom...</div>
            </div>
        `;

        try {
            const response = await fetch('/api/stoic/generate', { method: 'POST' });
            const data = await response.json();

            if (data.error) {
                content.innerHTML = `<div class="stoic-error">${data.error}</div>`;
                btn.disabled = false;
                btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Retry`;
                return;
            }

            stoicCardData = data;

            // Render card to canvas
            const canvas = await renderStoicCanvas(data);
            stoicImageData = canvas.toDataURL('image/png');

            content.innerHTML = `
                <div class="stoic-preview">
                    <img src="${stoicImageData}" alt="Stoic Card" style="width:100%;border-radius:8px;">
                </div>
                <div class="stoic-tweet-preview">
                    <div class="stoic-tweet-label">Tweet Text</div>
                    <div class="stoic-tweet-text">${data.tweet}</div>
                </div>
            `;

            // Update actions with posting buttons
            document.getElementById('stoicActions').innerHTML = `
                <div style="display:flex;flex-wrap:wrap;gap:0.5rem;width:100%;">
                    <button class="stoic-btn stoic-btn-cancel" onclick="closeStoicModal()" style="flex:0 0 auto;">Close</button>
                    <button class="stoic-btn" onclick="downloadStoicImage()" style="background:#374151;flex:0 0 auto;">⬇ Download</button>
                    <button class="btn-post-x" onclick="postStoicToX()" style="flex:1;">Post to 𝕏</button>
                    <button class="btn-post-instagram" onclick="postStoicToInstagram()">📸 Instagram</button>
                    <button class="btn-post-facebook" onclick="postStoicToFacebook()">📘 Facebook</button>
                    <button class="stoic-btn stoic-btn-queue" onclick="queueStoicCard()" style="flex:1;">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M12 5v14M5 12h14"/>
                        </svg>
                        Add to Queue
                    </button>
                </div>
            `;
        } catch (err) {
            content.innerHTML = `<div class="stoic-error">Failed to generate: ${err.message}</div>`;
            btn.disabled = false;
            btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Retry`;
        }
    }

    async function postStoicToX() {
        if (!stoicCardData) return;
        // Open tweet intent with the text
        const text = encodeURIComponent(stoicCardData.tweet);
        window.open(`https://twitter.com/intent/tweet?text=${text}`, '_blank');
        showToast('Opening X to post...');
    }

    function downloadStoicImage() {
        if (!stoicImageData) {
            showToast('No image to download', true);
            return;
        }

        // Create download link
        const link = document.createElement('a');
        link.href = stoicImageData;

        // Generate filename with date
        const now = new Date();
        const dateStr = now.toISOString().split('T')[0];
        link.download = `stoic-card-${dateStr}.png`;

        // Trigger download
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        showToast('Image downloaded!');
    }

    async function postStoicToInstagram() {
        if (!stoicCardData || !stoicImageData) return;

        const btn = event.target;
        const originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = 'Uploading...';

        try {
            // Upload to Cloudinary
            const uploadRes = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ image: stoicImageData })
            });
            const uploadData = await uploadRes.json();

            if (uploadData.error) throw new Error(uploadData.error);

            btn.innerHTML = 'Posting...';

            // Post to Instagram
            const postRes = await fetch('/api/post/instagram', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_url: uploadData.url,
                    caption: stoicCardData.tweet
                })
            });
            const postData = await postRes.json();

            if (postData.error) throw new Error(postData.error);

            showToast('Posted to Instagram!');
            btn.innerHTML = '✓ Posted';
        } catch (err) {
            showToast('Instagram error: ' + err.message, true);
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    }

    async function postStoicToFacebook() {
        if (!stoicCardData || !stoicImageData) return;

        const btn = event.target;
        const originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = 'Uploading...';

        try {
            // Upload to Cloudinary
            const uploadRes = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ image: stoicImageData })
            });
            const uploadData = await uploadRes.json();

            if (uploadData.error) throw new Error(uploadData.error);

            btn.innerHTML = 'Posting...';

            // Post to Facebook
            const postRes = await fetch('/api/post/facebook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    content: stoicCardData.tweet,
                    image_url: uploadData.url
                })
            });
            const postData = await postRes.json();

            if (postData.error) throw new Error(postData.error);

            showToast('Posted to Facebook!');
            btn.innerHTML = '✓ Posted';
        } catch (err) {
            showToast('Facebook error: ' + err.message, true);
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    }

    async function queueStoicCard() {
        if (!stoicCardData || !stoicImageData) return;

        const btn = event.target;
        const originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = 'Uploading image...';

        try {
            // First upload image to Cloudinary so it persists
            const uploadRes = await fetch('/api/cloudinary/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ image: stoicImageData })
            });
            const uploadData = await uploadRes.json();

            if (uploadData.error) {
                throw new Error(uploadData.error);
            }

            btn.innerHTML = 'Adding to queue...';

            // Queue with image URL
            const response = await fetch('/api/stoic/queue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tweet: stoicCardData.tweet,
                    image_url: uploadData.url
                })
            });
            const data = await response.json();

            if (data.error) {
                showToast('Failed to queue: ' + data.error, true);
                btn.disabled = false;
                btn.innerHTML = originalText;
                return;
            }

            // Add card to pending column with image data
            const pendingColumn = document.querySelector('.col-pending .column-body');
            const newCard = document.createElement('div');
            newCard.className = 'card pending';
            newCard.dataset.type = 'post';
            newCard.dataset.id = data.post_id;
            newCard.dataset.fullContent = JSON.stringify(stoicCardData.tweet);
            newCard.dataset.imageUrl = uploadData.url;
            newCard.innerHTML = `
                <div class="card-content">${stoicCardData.tweet.substring(0, 140)}${stoicCardData.tweet.length > 140 ? '...' : ''}</div>
                <div class="card-meta post-meta">
                    <span><span class="status-dot pending"></span>Stoic Card</span>
                    <span class="char-count char-ok">${stoicCardData.tweet.length}/280</span>
                </div>
            `;
            pendingColumn.insertBefore(newCard, pendingColumn.firstChild);

            // Update count
            const countEl = document.querySelector('.col-pending .column-count');
            countEl.textContent = parseInt(countEl.textContent) + 1;

            closeStoicModal();
            showToast('Stoic card added to queue!');
        } catch (err) {
            showToast('Failed to queue: ' + err.message, true);
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    }

    // Close stoic modal on backdrop click and escape
    document.getElementById('stoicModal').addEventListener('click', (e) => {
        if (e.target.id === 'stoicModal') closeStoicModal();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.getElementById('stoicModal').classList.contains('show')) {
            closeStoicModal();
        }
    });

    function resetUploadForm() {
        selectedFile = null;
        document.getElementById('uploadForm').style.display = 'block';
        document.getElementById('uploadProcessing').classList.remove('show');
        document.getElementById('uploadResult').classList.remove('show');
        document.getElementById('fileInfo').classList.remove('show');
        document.getElementById('fileInput').value = '';
        document.getElementById('btnExtract').disabled = true;
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function handleFileSelect(file) {
        if (!file) return;

        const ext = file.name.split('.').pop().toLowerCase();
        if (!['pdf', 'docx'].includes(ext)) {
            showToast('Please select a PDF or DOCX file', true);
            return;
        }

        selectedFile = file;
        document.getElementById('fileExt').textContent = ext.toUpperCase();
        document.getElementById('fileName').textContent = file.name;
        document.getElementById('fileSize').textContent = formatFileSize(file.size);
        document.getElementById('fileInfo').classList.add('show');
        document.getElementById('btnExtract').disabled = false;
    }

    function removeFile() {
        selectedFile = null;
        document.getElementById('fileInfo').classList.remove('show');
        document.getElementById('fileInput').value = '';
        document.getElementById('btnExtract').disabled = true;
    }

    async function extractQuotes() {
        if (!selectedFile) return;

        document.getElementById('uploadForm').style.display = 'none';
        document.getElementById('uploadProcessing').classList.add('show');

        const formData = new FormData();
        formData.append('file', selectedFile);

        try {
            const response = await fetch('/api/extract-quotes', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            document.getElementById('uploadProcessing').classList.remove('show');
            document.getElementById('uploadResult').classList.add('show');

            if (response.ok) {
                document.getElementById('resultIcon').className = 'result-icon success';
                document.getElementById('resultIcon').innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>';
                document.getElementById('resultTitle').textContent = 'Quotes Extracted!';
                document.getElementById('resultExtracted').textContent = data.extracted || 0;
                document.getElementById('resultSaved').textContent = data.saved || 0;
            } else {
                document.getElementById('resultIcon').className = 'result-icon error';
                document.getElementById('resultIcon').innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12"/></svg>';
                document.getElementById('resultTitle').textContent = data.error || 'Extraction failed';
                document.getElementById('resultExtracted').textContent = '0';
                document.getElementById('resultSaved').textContent = '0';
            }
        } catch (err) {
            document.getElementById('uploadProcessing').classList.remove('show');
            document.getElementById('uploadResult').classList.add('show');
            document.getElementById('resultIcon').className = 'result-icon error';
            document.getElementById('resultIcon').innerHTML = '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12"/></svg>';
            document.getElementById('resultTitle').textContent = 'Connection error';
            document.getElementById('resultExtracted').textContent = '0';
            document.getElementById('resultSaved').textContent = '0';
        }
    }

    function finishUpload() {
        closeUploadModal();
        location.reload();
    }

    // Generate consistent color from source name
    function getSourceColor(source) {
        let hash = 0;
        for (let i = 0; i < source.length; i++) {
            hash = source.charCodeAt(i) + ((hash << 5) - hash);
        }

        // Generate pleasant, distinct hues avoiding muddy colors
        const hue = Math.abs(hash) % 360;
        const saturation = 65 + (Math.abs(hash >> 8) % 20);
        const lightness = 55 + (Math.abs(hash >> 16) % 15);

        return {
            bg: `hsla(${hue}, ${saturation}%, ${lightness}%, 0.15)`,
            text: `hsl(${hue}, ${saturation}%, ${lightness}%)`
        };
    }

    // Apply colors to all source tags
    function applySourceColors() {
        document.querySelectorAll('.tag-source[data-source]').forEach(tag => {
            const source = tag.dataset.source;
            if (source) {
                const colors = getSourceColor(source);
                tag.style.backgroundColor = colors.bg;
                tag.style.color = colors.text;
            }
        });
    }

    // Shuffle cards with animation
    function shuffleCards() {
        const btn = document.querySelector('.btn-shuffle');
        const colBody = document.querySelector('.col-quotes .column-body');
        const cards = Array.from(colBody.querySelectorAll('.card'));

        if (cards.length < 2) return;

        // Add spinning animation to button
        btn.classList.add('shuffling');

        // Fade out cards
        cards.forEach(card => {
            card.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
            card.style.opacity = '0.3';
            card.style.transform = 'scale(0.95)';
        });

        setTimeout(() => {
            // Fisher-Yates shuffle
            for (let i = cards.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [cards[i], cards[j]] = [cards[j], cards[i]];
            }

            // Reorder in DOM
            cards.forEach((card, index) => {
                card.style.transitionDelay = `${index * 30}ms`;
                colBody.appendChild(card);
            });

            // Fade back in with stagger
            requestAnimationFrame(() => {
                cards.forEach((card, index) => {
                    card.style.transitionDelay = `${index * 40}ms`;
                    card.style.opacity = '1';
                    card.style.transform = 'scale(1)';
                });
            });

            // Cleanup
            setTimeout(() => {
                cards.forEach(card => {
                    card.style.transition = '';
                    card.style.transitionDelay = '';
                });
                btn.classList.remove('shuffling');
            }, 500);
        }, 150);
    }

    Object.assign(window, {
        openImageGenerator,
        closeImageGenerator,
        setTheme,
        setDimension,
        downloadImage,
        openInstagram,
        openLinkedIn,
        postToFacebook,
        openDraftModal,
        closeDraftModal,
        openEditDraftFromModal,
        deleteDraftFromModal,
        selectDraftImage,
        removeDraftImage,
        submitDraft,
        openUploadModal,
        closeUploadModal,
        removeFile,
        extractQuotes,
        finishUpload,
        openStoicModal,
        closeStoicModal,
        generateStoicCard,
        downloadStoicImage,
        postStoicToX,
        postStoicToInstagram,
        postStoicToFacebook,
        queueStoicCard,
        postToTwitterFromModal,
        postToInstagramFromModal,
        postToFacebookFromModal,
        postToLinkedInFromModal,
    });

    // File input and drag-drop handlers
    document.addEventListener('DOMContentLoaded', function() {
        // Apply source colors on load
        applySourceColors();
        syncBoardStats();

        const newDraftButton = document.getElementById('newDraftButton');
        const uploadZone = document.getElementById('uploadZone');
        const fileInput = document.getElementById('fileInput');
        const draftImageInput = document.getElementById('draftImageFile');

        if (newDraftButton) {
            newDraftButton.addEventListener('click', openDraftModal);
        }

        if (draftImageInput) {
            draftImageInput.addEventListener('change', handleDraftImageFile);
        }

        uploadZone.addEventListener('click', () => fileInput.click());

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length) handleFileSelect(e.target.files[0]);
        });

        uploadZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadZone.classList.add('drag-over');
        });

        uploadZone.addEventListener('dragleave', () => {
            uploadZone.classList.remove('drag-over');
        });

        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadZone.classList.remove('drag-over');
            if (e.dataTransfer.files.length) handleFileSelect(e.dataTransfer.files[0]);
        });

        // Close modal on backdrop click
        document.getElementById('uploadModal').addEventListener('click', (e) => {
            if (e.target.id === 'uploadModal') closeUploadModal();
        });

        // Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && document.getElementById('uploadModal').classList.contains('show')) {
                closeUploadModal();
            }
        });
    });
    </script>
</body>
</html>
"""


@app.route('/')
@login_required
def dashboard():
    if _needs_onboarding():
        return render_template_string(
            ONBOARDING_TEMPLATE,
            app_base_path=APP_BASE_PATH,
            providers=_render_provider_payload(),
        )

    init_db()
    db_session = get_session()

    scheduled_posts = db_session.query(Post).filter(
        Post.status == PostStatus.APPROVED.value
    ).order_by(Post.scheduled_time.asc()).all()

    pending_posts = db_session.query(Post).filter(
        Post.status == PostStatus.PENDING.value
    ).order_by(Post.scheduled_time.asc()).all()

    posted_posts = db_session.query(Post).filter(
        Post.status == PostStatus.POSTED.value
    ).order_by(Post.posted_time.desc()).limit(20).all()

    stats = {
        'pending': db_session.query(Post).filter(Post.status == PostStatus.PENDING.value).count(),
        'scheduled': db_session.query(Post).filter(Post.status == PostStatus.APPROVED.value).count(),
        'posted': db_session.query(Post).filter(Post.status == PostStatus.POSTED.value).count(),
    }

    return render_template_string(
        _dashboard_template(),
        pending_posts=pending_posts,
        scheduled_posts=scheduled_posts,
        posted_posts=posted_posts,
        stats=stats,
        profile=PROFILE_CONFIG,
        brand_config=_get_brand_config(),
        app_base_path=APP_BASE_PATH,
        platform_label=_platform_label,
    )


@app.route('/settings')
@login_required
def settings():
    return render_template_string(
        SETTINGS_TEMPLATE,
        app_base_path=APP_BASE_PATH,
        providers=_render_provider_payload(),
        onboarding=_needs_onboarding(),
        agent_token=os.getenv('SOCIAL_KANBAN_AGENT_TOKEN', ''),
    )


@app.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify({
        'success': True,
        'providers': _render_provider_payload(),
    })


@app.route('/api/settings', methods=['POST'])
@login_required
def save_settings():
    data = request.json or {}
    SETTINGS_STORE.save({'providers': data.get('providers', {})})
    return jsonify({
        'success': True,
        'providers': _render_provider_payload(),
    })


@app.route('/api/settings/test/<provider>', methods=['POST'])
@login_required
def test_settings_provider(provider: str):
    if provider not in PROVIDER_DEFINITIONS:
        return jsonify({'configured': False, 'error': 'Unknown provider'}), 404

    data = request.json or {}
    try:
        result = _test_provider_connection(provider, data.get('values') or {})
        return jsonify(result), 200 if result.get('configured') else 400
    except Exception as e:
        logger.error(f"Provider connection test failed for {provider}: {e}")
        return jsonify({'configured': False, 'error': str(e)}), 400


@app.route('/api/posts', methods=['POST'])
@login_or_agent_required
def create_posts():
    init_db()
    data = request.json or {}
    content = data.get('content')
    status = str(data.get('status', PostStatus.PENDING.value)).lower()
    media_url = data.get('media_url')

    if not isinstance(content, str) or not content.strip():
        return jsonify({'error': 'content is required'}), 400
    if status not in {PostStatus.PENDING.value, PostStatus.APPROVED.value, PostStatus.POSTED.value, PostStatus.REJECTED.value, PostStatus.FAILED.value}:
        return jsonify({'error': 'Unsupported status'}), 400

    try:
        scheduled_at = _parse_scheduled_at(data.get('scheduled_at'))
        platforms = _normalize_platforms(data.get('platform'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    db_session = get_session()
    created: list[dict[str, Any]] = []
    for platform in platforms:
        post = Post(
            platform=platform,
            content=content.strip(),
            media_path=media_url if isinstance(media_url, str) and media_url.strip() else None,
            scheduled_time=scheduled_at,
            status=status,
            created_at=datetime.now(timezone.utc),
            approved_at=datetime.now(timezone.utc) if status == PostStatus.APPROVED.value else None,
        )
        db_session.add(post)
        db_session.flush()
        created.append(_serialize_post(post))

    db_session.commit()
    return jsonify({
        'success': True,
        'posts': created,
    }), 201


def _serialize_post(post: Post) -> dict[str, Any]:
    return {
        'id': post.id,
        'platform': post.platform,
        'status': post.status,
        'content': post.content or '',
        'media_url': post.media_path or '',
        'scheduled_at': post.scheduled_time.isoformat() if post.scheduled_time else None,
        'posted_at': post.posted_time.isoformat() if post.posted_time else None,
    }


@app.route('/api/post/<int:post_id>', methods=['PATCH', 'DELETE'])
@login_required
def mutate_post(post_id: int):
    db_session = get_session()
    post = db_session.query(Post).filter(Post.id == post_id).first()

    if not post:
        return jsonify({'error': 'Post not found'}), 404

    if request.method == 'DELETE':
        db_session.delete(post)
        db_session.commit()
        return jsonify({'success': True})

    data = request.json or {}
    content = data.get('content')
    if not isinstance(content, str) or not content.strip():
        return jsonify({'error': 'content is required'}), 400

    try:
        scheduled_at = _parse_scheduled_at(data.get('scheduled_at'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    media_url = data.get('media_url')

    post.content = content.strip()
    post.scheduled_time = scheduled_at
    post.media_path = media_url.strip() if isinstance(media_url, str) and media_url.strip() else None
    db_session.commit()

    return jsonify({
        'success': True,
        'post': _serialize_post(post),
    })


@app.route('/api/post/status', methods=['POST'])
@login_required
def update_post_status():
    data = request.json
    post_id = data.get('post_id')
    new_status = data.get('status')

    if not post_id or new_status not in ['pending', 'approved', 'posted']:
        return jsonify({'error': 'Invalid request'}), 400

    session = get_session()
    post = session.query(Post).filter(Post.id == post_id).first()
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    post.status = new_status
    if new_status == 'approved':
        post.approved_at = datetime.now(timezone.utc)
    elif new_status == 'posted':
        post.posted_time = datetime.now(timezone.utc)

    session.commit()
    return jsonify({'success': True})


@app.route('/api/quote/to-post', methods=['POST'])
@login_required
def quote_to_post():
    data = request.json
    quote_id = data.get('quote_id')
    status = data.get('status', 'pending')

    if not quote_id:
        return jsonify({'error': 'Missing quote_id'}), 400

    session = get_session()
    quote = session.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        return jsonify({'error': 'Quote not found'}), 404

    cfg = _get_brand_config()
    post_hashtags = cfg['hashtags']
    post_tagline = cfg['tagline']
    content = f'"{quote.content}"\n\n{post_tagline}\n\n{post_hashtags}'
    if len(content) > 280:
        max_len = 280 - len(f'"\n\n{post_tagline}\n\n{post_hashtags}') - 6
        content = f'"{quote.content[:max_len]}..."\n\n{post_tagline}\n\n{post_hashtags}'

    post = Post(
        quote_id=quote.id,
        platform="twitter",
        content=content,
        status=status,
        created_at=datetime.now(timezone.utc)
    )
    if status == 'approved':
        post.approved_at = datetime.now(timezone.utc)

    quote.used_count += 1
    session.add(post)
    session.commit()

    return jsonify({'success': True, 'post_id': post.id, 'content': content})


@app.route('/api/extract-quotes', methods=['POST'])
@login_required
def extract_quotes_from_upload():
    import os
    import tempfile

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['pdf', 'docx']:
        return jsonify({'error': 'Invalid file type. Use PDF or DOCX'}), 400

    try:
        from core.content_extractor import ContentExtractor

        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        try:
            extractor = ContentExtractor()
            extracted, saved = extractor.extract_and_save(tmp_path)
            return jsonify({
                'success': True,
                'extracted': extracted,
                'saved': saved
            })
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except ValueError as e:
        if 'GROQ_API_KEY' in str(e):
            return jsonify({'error': 'Groq is not configured yet. Save your Groq key in Settings first.'}), 500
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Extraction failed: {str(e)}'}), 500


@app.route('/api/post/tweet', methods=['POST'])
@login_required
def post_to_twitter():
    """Post an approved post to Twitter/X."""
    data = request.json
    post_id = data.get('post_id')

    if not post_id:
        return jsonify({'error': 'Missing post_id'}), 400

    session = get_session()
    post = session.query(Post).filter(Post.id == post_id).first()

    if not post:
        return jsonify({'error': 'Post not found'}), 404

    if post.status == PostStatus.POSTED.value:
        return jsonify({'error': 'Already posted'}), 400

    if post.status != PostStatus.APPROVED.value:
        return jsonify({'error': 'Post must be approved before posting'}), 400

    try:
        from integrations.twitter_client import TwitterClient
        client = TwitterClient(dry_run=False)

        if not client.is_configured():
            return jsonify({'error': 'Twitter API not configured'}), 500

        result = client.client.create_tweet(text=post.content)

        post.status = PostStatus.POSTED.value
        post.posted_time = datetime.now(timezone.utc)
        post.post_id = str(result.data['id'])
        session.commit()

        return jsonify({
            'success': True,
            'tweet_id': result.data['id'],
            'url': f"https://x.com/{_get_brand_config()['handle']}/status/{result.data['id']}"
        })

    except Exception as e:
        return jsonify({'error': f'Failed to post: {str(e)}'}), 500


@app.route('/api/twitter/verify', methods=['GET'])
@login_required
def verify_twitter():
    """Verify Twitter API credentials."""
    try:
        from integrations.twitter_client import TwitterClient
        client = TwitterClient(dry_run=False)

        if not client.is_configured():
            return jsonify({'configured': False, 'error': 'Twitter API not configured'})

        result = client.verify_credentials()
        return jsonify(result)

    except Exception as e:
        return jsonify({'configured': False, 'error': str(e)})


def _mark_post_published(post: Post | None, remote_post_id: str | None = None) -> None:
    if not post:
        return
    post.status = PostStatus.POSTED.value
    post.posted_time = datetime.now(timezone.utc)
    if remote_post_id:
        post.post_id = str(remote_post_id)


@app.route('/api/post/facebook', methods=['POST'])
@login_required
def post_to_facebook():
    """Post content to Facebook Page (with optional image)."""
    data = request.json
    post_id = data.get('post_id')
    content = data.get('content')
    image_url = data.get('image_url')

    if not post_id and not content:
        return jsonify({'error': 'Missing post_id or content'}), 400

    try:
        from integrations.facebook_client import FacebookClient
        client = FacebookClient()
        session = None
        post = None

        if not client.is_configured():
            return jsonify({'error': 'Facebook API not configured'}), 500

        if post_id:
            session = get_session()
            post = session.query(Post).filter(Post.id == post_id).first()
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            content = post.content
            image_url = image_url or post.media_path

        if image_url:
            result = client.post_image(image_url, content)
        else:
            result = client.post_text(content)

        _mark_post_published(post, result.get('post_id'))
        if session:
            session.commit()

        return jsonify({
            'success': True,
            'post_id': result.get('post_id'),
            'url': result.get('url')
        })

    except Exception as e:
        return jsonify({'error': f'Failed to post: {str(e)}'}), 500


@app.route('/api/facebook/verify', methods=['GET'])
@login_required
def verify_facebook():
    """Verify Facebook Page credentials."""
    try:
        from integrations.facebook_client import FacebookClient
        client = FacebookClient()
        result = client.verify_credentials()
        return jsonify(result)

    except Exception as e:
        return jsonify({'configured': False, 'error': str(e)})


@app.route('/api/post/instagram', methods=['POST'])
@login_required
def post_to_instagram():
    """Post image to Instagram."""
    data = request.json
    post_id = data.get('post_id')
    image_url = data.get('image_url')
    caption = data.get('caption', '')

    if not post_id and not image_url:
        return jsonify({'error': 'Missing image_url (must be publicly accessible)'}), 400

    try:
        from integrations.instagram_client import InstagramClient
        client = InstagramClient()
        session = None
        post = None

        if not client.is_configured():
            return jsonify({'error': 'Instagram API not configured'}), 500

        if post_id:
            session = get_session()
            post = session.query(Post).filter(Post.id == post_id).first()
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            caption = caption or post.content
            image_url = image_url or post.media_path

        if not image_url:
            return jsonify({'error': 'Missing image_url (must be publicly accessible)'}), 400

        result = client.post_image(image_url, caption)

        _mark_post_published(post, result.get('post_id'))
        if session:
            session.commit()

        return jsonify({
            'success': True,
            'post_id': result.get('post_id'),
            'url': result.get('url')
        })

    except Exception as e:
        return jsonify({'error': f'Failed to post: {str(e)}'}), 500


@app.route('/api/instagram/verify', methods=['GET'])
@login_required
def verify_instagram():
    """Verify Instagram connection."""
    try:
        from integrations.instagram_client import InstagramClient
        client = InstagramClient()
        result = client.verify_credentials()
        return jsonify(result)

    except Exception as e:
        return jsonify({'configured': False, 'error': str(e)})


@app.route('/api/post/linkedin', methods=['POST'])
@login_required
def post_to_linkedin():
    """Post content to LinkedIn, optionally with an image."""
    data = request.json
    post_id = data.get('post_id')
    content = data.get('content')
    image_url = data.get('image_url')

    if not post_id and not content:
        return jsonify({'error': 'Missing post_id or content'}), 400

    try:
        from integrations.linkedin_client import LinkedInClient
        client = LinkedInClient()
        session = None
        post = None

        if not client.is_configured():
            return jsonify({'error': 'LinkedIn API not configured'}), 500

        if post_id:
            session = get_session()
            post = session.query(Post).filter(Post.id == post_id).first()
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            content = content or post.content
            image_url = image_url or post.media_path

        if image_url:
            result = client.post_image(image_url, content or '')
        else:
            result = client.post_text(content or '')

        _mark_post_published(post, result.get('post_id'))
        if session:
            session.commit()

        return jsonify({
            'success': True,
            'post_id': result.get('post_id'),
            'url': result.get('url'),
            'image_urn': result.get('image_urn'),
        })

    except Exception as e:
        return jsonify({'error': f'Failed to post: {str(e)}'}), 500


@app.route('/api/cloudinary/upload', methods=['POST'])
@login_required
def upload_to_cloudinary():
    """Upload base64 image to Cloudinary."""
    data = request.json
    image_data = data.get('image')

    if not image_data:
        return jsonify({'error': 'Missing image data'}), 400

    try:
        from integrations.cloudinary_client import CloudinaryClient
        client = CloudinaryClient()

        if not client.is_configured():
            return jsonify({'error': 'Cloudinary not configured'}), 500

        result = client.upload_base64(image_data)
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


@app.route('/api/cloudinary/verify', methods=['GET'])
@login_required
def verify_cloudinary():
    """Verify Cloudinary credentials."""
    try:
        from integrations.cloudinary_client import CloudinaryClient
        client = CloudinaryClient()
        result = client.verify_credentials()
        return jsonify(result)

    except Exception as e:
        return jsonify({'configured': False, 'error': str(e)})


@app.route('/api/cloudinary/cleanup', methods=['POST'])
@login_required
def cleanup_cloudinary():
    """Delete images older than specified days. Use via cron job."""
    data = request.json or {}
    days = data.get('days', 14)
    secret = data.get('secret')

    cleanup_secret = os.getenv('CLEANUP_SECRET')
    if cleanup_secret and secret != cleanup_secret:
        return jsonify({'error': 'Invalid secret'}), 401

    try:
        from integrations.cloudinary_client import CloudinaryClient
        client = CloudinaryClient()

        if not client.is_configured():
            return jsonify({'error': 'Cloudinary not configured'}), 500

        result = client.cleanup_old_images(days=days)
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'Cleanup failed: {str(e)}'}), 500


@app.route('/api/post/social', methods=['POST'])
@login_required
def post_to_social():
    """Post to Facebook and Instagram with image.

    Expects:
        - image: base64 image data
        - caption: text caption with hashtags
        - platforms: list of platforms ['facebook', 'instagram']
    """
    data = request.json
    image_data = data.get('image')
    caption = data.get('caption', '')
    platforms = data.get('platforms', ['facebook', 'instagram'])

    results = {'facebook': None, 'instagram': None}
    errors = []

    # Upload image to Cloudinary first
    image_url = None
    if image_data:
        try:
            from integrations.cloudinary_client import CloudinaryClient
            cloud_client = CloudinaryClient()
            if cloud_client.is_configured():
                upload_result = cloud_client.upload_base64(image_data)
                image_url = upload_result.get('secure_url')
        except Exception as e:
            errors.append(f'Cloudinary upload failed: {str(e)}')

    # Post to Facebook
    if 'facebook' in platforms:
        try:
            from integrations.facebook_client import FacebookClient
            fb_client = FacebookClient()
            if fb_client.is_configured():
                if image_url:
                    result = fb_client.post_image(image_url, caption)
                else:
                    result = fb_client.post_text(caption)
                results['facebook'] = result
        except Exception as e:
            errors.append(f'Facebook: {str(e)}')

    # Post to Instagram (requires image)
    if 'instagram' in platforms and image_url:
        try:
            from integrations.instagram_client import InstagramClient
            ig_client = InstagramClient()
            if ig_client.is_configured():
                result = ig_client.post_image(image_url, caption)
                results['instagram'] = result
        except Exception as e:
            errors.append(f'Instagram: {str(e)}')
    elif 'instagram' in platforms and not image_url:
        errors.append('Instagram requires an image')

    return jsonify({
        'success': len(errors) == 0,
        'results': results,
        'image_url': image_url,
        'errors': errors if errors else None
    })


@app.route('/api/status')
@login_required
def api_status():
    """Show which optional services are configured."""
    provider_state = _render_provider_payload()
    services = {
        'database': 'postgresql' if os.getenv('DATABASE_URL') else 'sqlite',
        'anthropic': provider_state['anthropic']['configured'],
        'groq': provider_state['groq']['configured'],
        'twitter': provider_state['twitter']['configured'],
        'facebook': provider_state['facebook']['configured'],
        'instagram': provider_state['instagram']['configured'],
        'linkedin': provider_state['linkedin']['configured'],
        'cloudinary': provider_state['cloudinary']['configured'],
        'auth': bool(os.getenv('DASHBOARD_PASSWORD')),
    }
    return jsonify(services)


# =============================================================================
# Stoic Card API Endpoints
# =============================================================================

STOIC_DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'daily_stoic.json')
STOIC_ARCHIVE_PATH = os.path.join(os.path.dirname(__file__), 'stoic-archive')


def load_stoic_entries():
    """Load the Daily Stoic entries from JSON file."""
    try:
        with open(STOIC_DATA_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load stoic entries: {e}")
        return []


def get_stoic_entry_for_today():
    """Get today's stoic entry."""
    entries = load_stoic_entries()
    now = datetime.now()
    month = now.strftime('%B')
    day = now.day

    for entry in entries:
        if entry.get('month') == month and entry.get('day') == day:
            return entry
    return None


def generate_stoic_trading_content(entry):
    """Use Claude Sonnet API to generate practical content for stoic entry."""
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    cfg = _get_brand_config()
    prompt = f'''You are creating a Stoic wisdom card that applies ancient Stoic philosophy to practical daily life.

Today's entry from The Daily Stoic:
- Title: {entry['title']}
- Philosopher: {entry['author']}
- Quote: "{entry['quote']}"
- Reflection: {entry['body'][:1000]}

Create content for the card with these elements:

1. Three principles (each with title, stoic meaning, and practical application):
   - Title: 2-4 words, captures the essence
   - Meaning: Brief stoic interpretation (under 10 words)
   - Trading: Specific practical application (under 12 words)

2. Closing wisdom: A reflective sentence connecting stoicism to daily practice (under 20 words)

3. Key takeaway: A punchy, memorable line (under 10 words)

4. Tweet text: A tweet (under 250 chars) with the key insight. Include hashtags: {cfg['hashtags']} #stoic #philosophy

Respond in JSON format only:
{{
  "point1_title": "...",
  "point1_meaning": "...",
  "point1_trading": "...",
  "point2_title": "...",
  "point2_meaning": "...",
  "point2_trading": "...",
  "point3_title": "...",
  "point3_meaning": "...",
  "point3_trading": "...",
  "closing_wisdom": "...",
  "key_takeaway": "...",
  "tweet": "..."
}}'''

    import requests as req
    response = req.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"Claude API error: {response.status_code} - {response.text}")

    content = response.json()["content"][0]["text"]

    # Parse JSON from response
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find('{')
        end = content.rfind('}') + 1
        if start != -1 and end > start:
            return json.loads(content[start:end])
        raise ValueError("Could not parse JSON from Claude response")


def generate_stoic_card_html(entry, content, date_str):
    """Generate the stoic card HTML."""
    source = entry.get('source', '')
    if source and source.isupper():
        source = source.title()

    template = '''<!DOCTYPE html>
<html>
<head>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:1080px;height:1350px;background:#0F0F0F;margin:0;padding:0}}
body{{display:flex;justify-content:center;align-items:center;font-family:Georgia,serif}}
div.card{{width:800px;background:linear-gradient(145deg,#141414,#0a0a0a);border:1px solid #1a1a1a;border-radius:20px;padding:70px;color:#e6e6e6}}
.header{{text-align:center;margin-bottom:45px}}
.header h1{{font-size:16px;letter-spacing:5px;text-transform:uppercase;color:#C45A3B;margin-bottom:8px}}
.header .source{{font-size:12px;color:#666;margin-bottom:12px;font-style:italic}}
.header h2{{font-size:32px;color:#e6e6e6;font-style:italic;font-weight:normal}}
.date{{text-align:center;font-size:12px;letter-spacing:2px;color:#555;margin-bottom:20px;text-transform:uppercase}}
.divider{{height:1px;background:linear-gradient(90deg,transparent,#333,transparent);margin:30px 0}}
.area{{margin:28px 0;text-align:center}}
.area-title{{font-size:18px;color:#C45A3B;margin-bottom:8px;font-weight:bold}}
.area-meaning{{font-size:15px;color:#888;margin-bottom:6px;font-style:italic}}
.area-trading{{font-size:17px;color:#e6e6e6}}
.bottom{{text-align:center;margin-top:40px;padding-top:30px;border-top:1px solid #1a1a1a}}
.bottom p{{font-size:17px;color:#888;line-height:1.7;font-style:italic}}
.bottom .key{{color:#C45A3B;font-style:normal;font-weight:bold;display:block;margin-top:20px;font-size:18px}}
.cta{{text-align:center;margin-top:35px}}
.cta-text{{font-size:15px;color:#777;font-style:italic;margin-bottom:8px}}
.cta-url{{font-size:14px;color:#e6e6e6;letter-spacing:2px;font-weight:bold}}
</style>
</head>
<body>
<div class="card">
  <div class="date">{date}</div>
  <div class="header">
    <h1>{philosopher}</h1>
    <div class="source">{source}</div>
    <h2>{title}</h2>
  </div>
  <div class="divider"></div>
  <div class="area">
    <div class="area-title">1. {point1_title}</div>
    <div class="area-meaning">{point1_meaning}</div>
    <div class="area-trading">{point1_trading}</div>
  </div>
  <div class="area">
    <div class="area-title">2. {point2_title}</div>
    <div class="area-meaning">{point2_meaning}</div>
    <div class="area-trading">{point2_trading}</div>
  </div>
  <div class="area">
    <div class="area-title">3. {point3_title}</div>
    <div class="area-meaning">{point3_meaning}</div>
    <div class="area-trading">{point3_trading}</div>
  </div>
  <div class="bottom">
    <p>{closing_wisdom}</p>
    <span class="key">{key_takeaway}</span>
  </div>
  <div class="cta">
    <div class="cta-text">{tagline}</div>
    <div class="cta-url">{domain}</div>
  </div>
</div>
</body>
</html>'''

    cfg = _get_brand_config()
    return template.format(
        date=date_str,
        philosopher=entry['author'],
        source=source,
        title=entry['title'],
        point1_title=content['point1_title'],
        point1_meaning=content['point1_meaning'],
        point1_trading=content['point1_trading'],
        point2_title=content['point2_title'],
        point2_meaning=content['point2_meaning'],
        point2_trading=content['point2_trading'],
        point3_title=content['point3_title'],
        point3_meaning=content['point3_meaning'],
        point3_trading=content['point3_trading'],
        closing_wisdom=content['closing_wisdom'],
        key_takeaway=content['key_takeaway'],
        tagline=cfg['tagline'],
        domain=cfg['domain'],
    )


def html_to_png_stoic(html_content, output_path):
    """Convert HTML to PNG using Chrome headless."""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
        f.write(html_content)
        html_path = f.name

    try:
        chrome_paths = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
        ]

        chrome = None
        for path in chrome_paths:
            if os.path.exists(path):
                chrome = path
                break

        if not chrome:
            raise Exception("Chrome not found")

        cmd = [
            chrome,
            '--headless',
            '--disable-gpu',
            '--screenshot=' + str(output_path),
            '--window-size=1080,1350',
            '--hide-scrollbars',
            f'file://{html_path}'
        ]

        subprocess.run(cmd, check=True, capture_output=True)

    finally:
        os.unlink(html_path)

    return output_path


@app.route('/api/stoic/entry', methods=['GET'])
@login_required
def get_stoic_entry():
    """Get today's stoic entry info."""
    entry = get_stoic_entry_for_today()
    if not entry:
        return jsonify({'error': 'No entry found for today'}), 404

    return jsonify({
        'title': entry.get('title', ''),
        'author': entry.get('author', ''),
        'quote': entry.get('quote', '')[:200] + '...' if len(entry.get('quote', '')) > 200 else entry.get('quote', ''),
    })


@app.route('/api/stoic/generate', methods=['POST'])
@login_required
def generate_stoic_card():
    """Generate today's stoic card content (client renders the image)."""
    try:
        entry = get_stoic_entry_for_today()
        if not entry:
            return jsonify({'error': 'No stoic entry found for today'}), 404

        # Generate trading content via AI
        content = generate_stoic_trading_content(entry)

        # Format date
        now = datetime.now()
        date_str = f"{now.strftime('%B')} {now.day}, {now.year}"

        # Clean up source
        source = entry.get('source', '')
        if source and source.isupper():
            source = source.title()

        # Return all data for client-side rendering
        return jsonify({
            'success': True,
            'date': date_str,
            'title': entry['title'],
            'author': entry['author'],
            'source': source,
            'point1_title': content['point1_title'],
            'point1_meaning': content['point1_meaning'],
            'point1_trading': content['point1_trading'],
            'point2_title': content['point2_title'],
            'point2_meaning': content['point2_meaning'],
            'point2_trading': content['point2_trading'],
            'point3_title': content['point3_title'],
            'point3_meaning': content['point3_meaning'],
            'point3_trading': content['point3_trading'],
            'closing_wisdom': content['closing_wisdom'],
            'key_takeaway': content['key_takeaway'],
            'tweet': content['tweet']
        })

    except ValueError as e:
        if 'GROQ_API_KEY' in str(e):
            return jsonify({'error': 'Groq is not configured yet. Save your Groq key in Settings first.'}), 500
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Stoic card generation failed: {e}")
        return jsonify({'error': f'Generation failed: {str(e)}'}), 500


@app.route('/api/stoic/queue', methods=['POST'])
@login_required
def queue_stoic_card():
    """Add generated stoic card to the post queue."""
    data = request.json
    tweet = data.get('tweet')
    image_url = data.get('image_url')

    if not tweet:
        return jsonify({'error': 'Missing tweet text'}), 400

    try:
        db_session = get_session()

        # Create a new post with image URL
        post = Post(
            content=tweet,
            media_path=image_url,  # Store Cloudinary URL
            platform='twitter',
            status=PostStatus.PENDING.value,
            created_at=datetime.now(timezone.utc)
        )
        db_session.add(post)
        db_session.commit()

        return jsonify({
            'success': True,
            'post_id': post.id,
            'image_url': image_url
        })

    except Exception as e:
        logger.error(f"Failed to queue stoic card: {e}")
        return jsonify({'error': f'Queue failed: {str(e)}'}), 500


def ensure_db_seeded():
    """Seed sample data if database is empty."""
    if os.getenv('SOCIAL_KANBAN_SEED_SAMPLE_DATA', '').strip().lower() not in {'1', 'true', 'yes'}:
        return
    init_db()
    session = get_session()
    if session.query(Quote).count() == 0:
        from seed_sample_data import seed_quotes, seed_posts
        seed_quotes()
        seed_posts()
        print("Database seeded with sample data")

# Seed on import for production (gunicorn)
ensure_db_seeded()

if __name__ == '__main__':
    print("\n  Social Kanban: http://localhost:5001\n")
    app.run(debug=True, port=5001)
