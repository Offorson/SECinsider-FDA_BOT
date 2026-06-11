"""Entrypoint: weekly FDA "Week Ahead" calendar (Sunday 18:00 UTC).

Posts a scheduled catalyst calendar (not an alert) — so it goes out immediately,
no 24h free-channel delay. Two versions from one run:
  * PAID FDA channel : next-7-days view (+ ⚡ insider cross-signal) + a paid-only
    "Further out (next 30 days)" section.
  * FREE FDA channel : the 7-day view only, plus a Pro-tier call to action.
Plus a tap-to-copy "Copy for X" block (condensed, under 280 chars).

Month/quarter-precision catalysts (no exact day) are gathered into a separate
"Expected this month" line. If nothing is dated in the next 7 days it posts a
short "quiet week" note to paid only (never an empty calendar). Idempotent.

    python -m src.jobs.weekly_fda_calendar --dry-run
    python -m src.jobs.weekly_fda_calendar
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from src.core.config import load_config
from src.core.db import get_db, now_utc
from src.core.telegram import TelegramClient, TelegramError, escape_html
from src.core.alerting import RunStats, setup_logging

log = logging.getLogger("weekly_fda_calendar")


def _parse_d(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _has_insider_buying(db, ticker, since_iso):
    try:
        return bool(db.get_window_buys(ticker, since_iso))
    except Exception:  # noqa: BLE001
        return False


def _entry(c):
    """'$SPRO <i>Spero Therapeutics</i> · tebipenem HBr · PDUFA'"""
    seg = "$" + escape_html(c.get("ticker") or "?")
    if c.get("company"):
        seg += f" <i>{escape_html(c['company'])}</i>"
    parts = [seg]
    if c.get("drug"):
        parts.append(escape_html(c["drug"]))
    parts.append(escape_html(c.get("catalyst_type") or "catalyst"))
    return " · ".join(parts)


def _bucket(db, today, cfg):
    """Sort upcoming catalysts into dated/month buckets."""
    week_days = cfg.get_path("fda_calendar.week_days", 7)
    further_days = cfg.get_path("fda_calendar.further_out_days", 30)
    week_end = today + timedelta(days=week_days)
    horizon = today + timedelta(days=further_days)

    next7, further, this_month, later_month, all_up = [], [], [], [], []
    for c in db.get_upcoming_catalysts():
        d = _parse_d(c.get("expected_date"))
        if d is None:
            continue
        precision = (c.get("date_precision") or "day").lower()
        if precision == "day":
            if d < today:
                continue
            all_up.append((d, c))
            if d <= week_end:
                next7.append((d, c))
            elif d <= horizon:
                further.append((d, c))
        else:  # month / quarter precision -> no exact day
            if d.year == today.year and d.month == today.month:
                this_month.append(c)
                all_up.append((d, c))
            elif d >= today and d <= horizon:
                later_month.append((d, c))
                all_up.append((d, c))
            elif d >= today:
                all_up.append((d, c))
    next7.sort(key=lambda x: x[0])
    further.sort(key=lambda x: x[0])
    later_month.sort(key=lambda x: x[0])
    all_up.sort(key=lambda x: x[0])
    return {"next7": next7, "further": further, "this_month": this_month,
            "later_month": later_month, "all_up": all_up,
            "week_end": week_end}


def build_messages(db, today, cfg):
    """Return dict: paid (str), free (str|None), xcopy (str), quiet (bool)."""
    b = _bucket(db, today, cfg)
    week_end = b["week_end"]
    since = (today - timedelta(days=cfg.get_path("fda_calendar.cross_signal_days", 30))).isoformat()
    rng = f"<i>{today.strftime('%b %-d')} – {week_end.strftime('%b %-d')}</i>"

    # ---------------- quiet week (nothing dated in next 7 days) -------------
    if not b["next7"]:
        paid = [f"📅 <b>FDA Week Ahead</b>",
                "<i>Quiet week — no scheduled FDA catalysts in the next 7 days.</i>",
                "", "<b>Next up</b>"]
        for d, c in b["all_up"][:3]:
            when = d.strftime("%b %-d") if (c.get("date_precision") or "day") == "day" else d.strftime("%b %Y")
            paid.append(f"• <b>{when}</b> — {_entry(c)}")
        x = ["📅 FDA Week Ahead — quiet week"]
        for d, c in b["all_up"][:3]:
            x.append(f"${c.get('ticker')} {c.get('catalyst_type')} {d.strftime('%-m/%-d')}")
        x.append("Not investment advice.")
        return {"paid": "\n".join(paid), "free": None,
                "xcopy": _fit_x(x), "quiet": True}

    # ---------------- normal week ------------------------------------------
    def week_lines(with_cross):
        out = ["<b>This week</b>"]
        for d, c in b["next7"]:
            out.append(f"• <b>{d.strftime('%b %-d')}</b> — {_entry(c)}")
            if with_cross and _has_insider_buying(db, c.get("ticker"), since):
                out.append("   ⚡ <i>insider buying logged in the last 30 days</i>")
        return out

    def month_lines():
        if not b["this_month"]:
            return []
        out = ["", "<b>Expected this month</b>"]
        for c in b["this_month"]:
            out.append(f"• {_entry(c)}")
        return out

    # PAID: cross-signals + further-out section
    paid = [f"📅 <b>FDA Week Ahead</b>", rng, ""] + week_lines(True) + month_lines()
    if b["further"] or b["later_month"]:
        paid += ["", "<b>Further out (next 30 days)</b>"]
        for d, c in b["further"]:
            paid.append(f"• <b>{d.strftime('%b %-d')}</b> — {_entry(c)}")
            if _has_insider_buying(db, c.get("ticker"), since):
                paid.append("   ⚡ <i>insider buying logged in the last 30 days</i>")
        for d, c in b["later_month"]:
            paid.append(f"• <b>{d.strftime('%b %Y')}</b> — {_entry(c)}")

    # FREE: 7-day view only (no cross-signals) + Pro CTA
    free = [f"📅 <b>FDA Week Ahead</b>", rng, ""] + week_lines(False) + month_lines()
    free += ["", f"<i>{escape_html(cfg.get_path('fda_calendar.pro_cta', 'Upgrade to Pro for more.'))}</i>"]

    # X-copy (condensed 7-day, < 280 chars)
    x = [f"📅 FDA Week Ahead {today.strftime('%-m/%-d')}-{week_end.strftime('%-m/%-d')}"]
    for d, c in b["next7"]:
        x.append(f"${c.get('ticker')} {c.get('catalyst_type')} {d.strftime('%-m/%-d')}")
    x.append("Not investment advice.")
    return {"paid": "\n".join(paid), "free": "\n".join(free),
            "xcopy": _fit_x(x), "quiet": False}


def _fit_x(lines, limit=280):
    foot = lines[-1]
    text = lines[0]
    for line in lines[1:-1]:
        if len(text + "\n" + line + "\n" + foot) <= limit:
            text += "\n" + line
        else:
            text += "\n…"
            break
    return text + "\n" + foot


def _post(db, telegram, channel, dedup_key, html, stale, stats, label):
    if not channel:
        log.warning("No channel for %s — skipping.", label)
        return
    if not db.claim_alert(dedup_key, "fda", channel, "week_ahead", {}, stale):
        stats.inc("skipped_" + label)
        return
    try:
        telegram.send(channel, html)
    except TelegramError as exc:
        db.release_alert(dedup_key)
        log.error("post failed (%s): %s", label, exc)
        stats.inc("send_failed")
        return
    db.confirm_alert(dedup_key)
    stats.inc("posted_" + label)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly FDA Week Ahead calendar")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("weekly_fda_calendar")
    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key", "telegram_bot_token")

    db = get_db(cfg, dry_run=args.dry_run)
    try:
        db.healthcheck()
    except Exception as exc:  # noqa: BLE001
        log.error("Supabase unreachable (%s) — exiting 1.", exc)
        return 1

    today = now_utc().date()
    msgs = build_messages(db, today, cfg)

    telegram = TelegramClient(
        bot_token=cfg.secrets.telegram_bot_token,
        disclaimer=cfg["delivery"]["disclaimer"],
        max_retries=cfg["delivery"]["telegram_max_retries"],
        backoff_base_sec=cfg["delivery"]["telegram_backoff_base_sec"],
        dry_run=args.dry_run,
    )
    ch = cfg["delivery"]["channels"]
    stale = cfg.get_path("idempotency.pending_stale_minutes", 30)

    _post(db, telegram, ch.get("fda_paid"), f"fda:weekahead:{today}:paid",
          msgs["paid"], stale, stats, "paid")
    if msgs["free"]:
        _post(db, telegram, ch.get("fda_free"), f"fda:weekahead:{today}:free",
              msgs["free"], stale, stats, "free")
    xcopy = "📋 <b>Copy for X</b>\n<pre>" + escape_html(msgs["xcopy"]) + "</pre>"
    _post(db, telegram, ch.get("fda_paid"), f"fda:weekahead:{today}:xcopy",
          xcopy, stale, stats, "xcopy")

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
