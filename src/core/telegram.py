"""Telegram delivery layer.

The bot only POSTS to channels (no incoming-message handling), so this is plain
HTTPS via ``requests`` — no bot framework. Responsibilities:

* HTML parse mode; dynamic text must already be escaped by the caller via
  ``escape_html`` (structural <b>/<a> tags are intentional and not escaped).
* Guarantee EVERY message ends with the disclaimer footer (appended here exactly
  once, structurally — so no code path can forget it).
* Retry with exponential backoff, honouring Telegram's ``retry_after`` on 429.
* ``dry_run`` prints to stdout instead of calling the API, so the whole pipeline
  is testable with no token and no network.
"""
from __future__ import annotations

import html
import logging
import time
from typing import Optional

import requests

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"


def escape_html(text: str) -> str:
    """Escape user/dynamic text for Telegram HTML parse mode."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


class TelegramError(Exception):
    """Raised when a post ultimately fails after all retries."""


class TelegramClient:
    def __init__(self, bot_token: str, disclaimer: str,
                 max_retries: int = 5, backoff_base_sec: float = 2.0,
                 timeout_sec: int = 20, dry_run: bool = False) -> None:
        self.bot_token = bot_token
        self.disclaimer = disclaimer.strip()
        self.max_retries = max_retries
        self.backoff_base_sec = backoff_base_sec
        self.timeout_sec = timeout_sec
        self.dry_run = dry_run
        self._session = requests.Session()

    def _with_footer(self, html_text: str) -> str:
        body = (html_text or "").rstrip()
        if self.disclaimer and self.disclaimer in body:
            return body
        return f"{body}\n\n{self.disclaimer}"

    def send(self, channel_id: str, html_text: str,
             disable_preview: bool = True) -> bool:
        """Post a message. Returns True on success.

        Raises TelegramError after exhausting retries so the caller can leave the
        alert un-confirmed (queued) rather than marking it sent.
        """
        message = self._with_footer(html_text)

        if self.dry_run:
            print("\n" + "=" * 64)
            print(f"[DRY-RUN] would post to channel {channel_id!r}:")
            print("-" * 64)
            print(message)
            print("=" * 64)
            return True

        if not self.bot_token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is empty; cannot post.")
        if not channel_id:
            raise TelegramError("Empty channel id; check config.yaml / env.")

        url = f"{API_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": channel_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }

        last_err: Optional[str] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=self.timeout_sec)
                if resp.status_code == 200:
                    return True
                if resp.status_code == 429:
                    retry_after = 1
                    try:
                        retry_after = int(resp.json()
                                          .get("parameters", {})
                                          .get("retry_after", 1))
                    except Exception:  # noqa: BLE001
                        pass
                    wait = max(retry_after, self.backoff_base_sec ** attempt)
                    log.warning("Telegram 429; sleeping %.1fs (attempt %d/%d)",
                                wait, attempt, self.max_retries)
                    time.sleep(wait)
                    last_err = "429 rate limited"
                    continue
                # Other HTTP errors: backoff and retry (could be transient 5xx).
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.warning("Telegram post failed (%s); retrying", last_err)
            except requests.RequestException as exc:
                last_err = str(exc)
                log.warning("Telegram network error: %s; retrying", last_err)

            time.sleep(self.backoff_base_sec ** attempt)

        raise TelegramError(f"Failed after {self.max_retries} retries: {last_err}")
