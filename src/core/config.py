"""Configuration + secrets loader.

Loads ``config.yaml`` (all tunables, NO secrets) and resolves secrets from
environment variables. Any string in the YAML may reference an env var with
``${VAR_NAME}``; it is expanded here at load time so non-secret pointers (channel
IDs, the SEC user-agent) live in env rather than the committed repo.

Usage::

    from src.core.config import load_config
    cfg = load_config()
    window = cfg["sec"]["cluster"]["window_days"]
    secrets = cfg.secrets          # attribute access for required secrets
"""
from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is only needed for local .env runs; optional in CI.
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("config")

# Project root = two levels up from this file (src/core/config.py -> repo root).
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings using os.environ."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


class Secrets:
    """Lazily-validated access to environment secrets.

    Reading an attribute that is required-but-missing raises a clear error, so a
    misconfigured deployment fails loudly instead of sending nothing silently.
    Gemini is optional (template fallback), so it is allowed to be empty.
    """

    def __init__(self) -> None:
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.sec_app_name = os.environ.get("SEC_APP_NAME", "SecBiotechBot").strip()
        self.sec_contact_email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()

    @property
    def sec_user_agent(self) -> str:
        """SEC-required User-Agent: 'AppName contact@email'."""
        email = self.sec_contact_email or "unknown@example.com"
        return f"{self.sec_app_name} {email}"

    def require(self, *names: str) -> None:
        """Raise if any named secret is missing. Call at job start."""
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise RuntimeError(
                "Missing required environment secrets: "
                + ", ".join(m.upper() for m in missing)
                + ". See .env.example."
            )


class Config(dict):
    """Plain dict of tunables with a ``.secrets`` handle attached."""

    secrets: Secrets

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load and return the merged config object."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    expanded = _expand_env(raw)
    cfg = Config(expanded)
    cfg.secrets = Secrets()
    return cfg
