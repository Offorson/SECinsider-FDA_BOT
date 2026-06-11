"""Entrypoint: daily job.

Three things, all idempotent (one set per calendar day, guarded by alerts_sent):
1. FDA reminders  — T-30 / T-14 / T-7 / T-1 before each upcoming catalyst.
2. FDA overdue    — passed PDUFA with no news -> mark passed-unresolved + alert.
3. Digest         — a factual roundup of upcoming catalysts (next 30 days) and
                    currently-active insider clusters, to the relevant channels.

    python -m src.jobs.daily_digest --dry-run
    python -m src.jobs.daily_digest

Exit 0 normally; 1 only on Supabase outage.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from src.core.config import load_config
from src.core.db import get_db, now_utc
from src.core.telegram import TelegramClient, escape_html
from src.core.summarizer import Summarizer
from src.core.alerting import AlertDispatcher, RunStats, setup_logging
from src.feeds.fda_catalysts import FdaCatalystFeed

log = logging.getLogger("daily_digest")

DIGEST_WINDOW_DAYS = 30


def _build_fda_digest(db, today) -> str | None:
    cats = db.get_upcoming_catalysts()
    horizon = today + timedelta(days=DIGEST_WINDOW_DAYS)
    upcoming = []
    for c in cats:
        ed = c.get("expected_date")
        if not ed:
            continue
        try:
            d = datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= horizon:
            upcoming.append((d, c))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    lines = [f"🗓️ <b>Upcoming Catalysts · Next {DIGEST_WINDOW_DAYS} Days</b>", ""]
    for d, c in upcoming:
        drug = f" {escape_html(c['drug'])}" if c.get("drug") else ""
        lines.append(f"• <b>{d.strftime('%b %-d')}</b> — "
                     f"${escape_html(c.get('ticker') or '?')}{drug} "
                     f"<i>{escape_html(c.get('catalyst_type') or '')}</i>")
    return "\n".join(lines)


def _build_sec_digest(db) -> str | None:
    clusters = db.get_active_clusters()
    if not clusters:
        return None
    clusters.sort(key=lambda c: -(c.get("combined_value") or 0))
    lines = ["📊 <b>Active Insider Clusters</b>", ""]
    for c in clusters:
        lines.append(f"• <b>${escape_html(c.get('ticker') or '?')}</b> — "
                     f"{c.get('member_count')} insiders · "
                     f"<b>${(c.get('combined_value') or 0):,.0f}</b> combined")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily digest + reminders")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("daily_digest")

    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key", "telegram_bot_token")

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
    fda_disp = AlertDispatcher(db, telegram, cfg, feed="fda", stats=stats)
    sec_disp = AlertDispatcher(db, telegram, cfg, feed="sec", stats=stats)
    feed = FdaCatalystFeed(cfg)

    # 1 + 2: reminders and overdue.
    feed.run_reminders(db, fda_disp, summarizer, stats)
    feed.run_overdue(db, fda_disp, summarizer, stats)

    # 3: digests (one per day, idempotent via dated dedup key).
    today = now_utc().date()
    ch = cfg["delivery"]["channels"]

    fda_digest = _build_fda_digest(db, today)
    if fda_digest:
        if fda_disp.dispatch(f"fda:digest:{today}", "digest",
                             ch["fda_paid"], ch["fda_free"], fda_digest):
            stats.inc("fda_digest")

    sec_digest = _build_sec_digest(db)
    if sec_digest:
        if sec_disp.dispatch(f"sec:digest:{today}", "digest",
                             ch["sec_paid"], ch["sec_free"], sec_digest):
            stats.inc("sec_digest")

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
