"""Entrypoint: poll SEC EDGAR for Form 4 cluster/large-buy alerts.

Run by GitHub Actions every ~10 minutes. Also runnable locally:

    python -m src.jobs.poll_sec --dry-run                 # live EDGAR, print only
    python -m src.jobs.poll_sec --dry-run --fixtures tests/fixtures/sec
    python -m src.jobs.poll_sec                           # live: posts to Telegram

Exit codes (per spec graceful-degradation rules):
* 0  normal, or EDGAR upstream outage (no false CI failures)
* 1  Supabase unreachable (a real failure worth surfacing)
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.core.config import load_config
from src.core.db import get_db
from src.core.telegram import TelegramClient
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.sec_form4 import SecForm4Feed, EdgarSource, FixtureSource, EdgarUnavailable

log = logging.getLogger("poll_sec")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SEC Form 4 cluster bot poller")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the full pipeline but print alerts instead of posting")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--fixtures", metavar="DIR",
                    help="use local fixture filings instead of live EDGAR (offline)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max new filings to process this run")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("poll_sec")

    use_fixtures = bool(args.fixtures)

    # Secret requirements depend on mode.
    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key", "telegram_bot_token")
    if not use_fixtures:
        # Hitting live EDGAR -> the descriptive User-Agent email is mandatory.
        if not cfg.secrets.sec_contact_email:
            log.error("SEC_CONTACT_EMAIL is required to query EDGAR. "
                      "Set it or pass --fixtures for offline runs.")
            return 1

    db = get_db(cfg, dry_run=args.dry_run)
    try:
        db.healthcheck()
    except Exception as exc:  # noqa: BLE001
        log.error("Supabase unreachable (%s) — exiting 1.", exc)
        return 1

    telegram = TelegramClient(
        bot_token=cfg.secrets.telegram_bot_token,
        disclaimer=cfg["delivery"]["disclaimer"],
        max_retries=cfg["delivery"]["telegram_max_retries"],
        backoff_base_sec=cfg["delivery"]["telegram_backoff_base_sec"],
        dry_run=args.dry_run,
    )
    summarizer = Summarizer(
        backend=cfg["summarizer"]["backend"],
        gemini_api_key=cfg.secrets.gemini_api_key,
        gemini_model=cfg["summarizer"].get("gemini_model", "gemini-1.5-flash"),
    )
    dispatcher = AlertDispatcher(db, telegram, cfg, feed="sec", stats=stats)

    if use_fixtures:
        source = FixtureSource(args.fixtures)
    else:
        source = EdgarSource(cfg, user_agent=cfg.secrets.sec_user_agent)

    feed = SecForm4Feed(cfg)
    try:
        feed.run(db, dispatcher, summarizer, stats, source, limit=args.limit)
    except EdgarUnavailable as exc:
        log.warning("EDGAR unavailable (%s) — exiting 0 (upstream outage).", exc)
        stats.log()
        return 0

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
