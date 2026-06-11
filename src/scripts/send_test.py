"""Manual Telegram delivery test.

Posts a sample message to all four channels so you can confirm the bot token and
channel IDs are correct (and that the bot is an admin of each channel). Run it
from the 'test-telegram' GitHub workflow (Run workflow), or locally:

    python -m src.scripts.send_test
    python -m src.scripts.send_test --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from src.core.config import load_config
from src.core.telegram import TelegramClient, TelegramError, escape_html
from src.core.alerting import setup_logging

log = logging.getLogger("send_test")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Post a test message to all channels")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    if not args.dry_run:
        cfg.secrets.require("telegram_bot_token")

    tg = TelegramClient(
        bot_token=cfg.secrets.telegram_bot_token,
        disclaimer=cfg["delivery"]["disclaimer"],
        max_retries=cfg["delivery"]["telegram_max_retries"],
        backoff_base_sec=cfg["delivery"]["telegram_backoff_base_sec"],
        dry_run=args.dry_run,
    )
    channels = cfg["delivery"]["channels"]
    order = [("SEC paid", "sec_paid"), ("SEC free", "sec_free"),
             ("FDA paid", "fda_paid"), ("FDA free", "fda_free")]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    failures = 0
    for label, key in order:
        chan = channels.get(key, "")
        if not chan:
            log.error("%s channel (%s) is empty in config/secrets - skipping.", label, key)
            failures += 1
            continue
        html = ("✅ <b>Telegram delivery test</b>\n"
                "This is a test from your SEC + FDA alert bot. If you can read "
                "this, the <b>%s</b> channel is wired up correctly.\n"
                "Sent %s." % (escape_html(label), escape_html(ts)))
        try:
            tg.send(chan, html)
            log.info("OK   -> %s (%s)", label, chan)
        except TelegramError as exc:
            failures += 1
            log.error("FAIL -> %s (%s): %s", label, chan, exc)

    if failures:
        log.error("%d channel(s) failed or empty. Check the channel IDs/usernames "
                  "and that the bot is an ADMIN of each channel.", failures)
        return 1
    log.info("All four channels delivered. Telegram is wired up correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
