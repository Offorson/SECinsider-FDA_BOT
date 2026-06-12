"""Entrypoint: weekly performance recap (Friday after US close).

For every recorded SEC alert in the lookback window it pulls the ticker's price
history from a free source (Stooq), computes the close on the alert date and the
% return at 1-week / 1-month / 3-month milestones (where enough time has passed),
persists those to ``alert_performance``, then posts a recap to the paid channel
plus a tap-to-copy "Copy for X" block. Winners AND losers, no filtering.

    python -m src.jobs.weekly_recap --dry-run
    python -m src.jobs.weekly_recap

Exit 0 normally; 1 only on Supabase outage. Idempotent (one recap per day).
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
from src.core import prices

log = logging.getLogger("weekly_recap")

WINDOWS = [("1W", 7), ("1M", 30), ("3M", 90)]


def _to_date(v):
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _fmt_pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def price_and_score(db, source, today, rows, stats):
    """Price every alert row, persist results, return a list of result dicts."""
    cache: dict[str, list] = {}
    results = []
    for r in rows:
        ticker = r["ticker"]
        adate = _to_date(r["alert_date"])
        if ticker not in cache:
            cache[ticker] = source.history(ticker)
        hist = cache[ticker]
        if not hist:
            stats.inc("no_price")
        alert_price = prices.close_asof(hist, adate)
        if alert_price is None and r.get("alert_price"):
            alert_price = float(r["alert_price"])
        last = prices.last_close(hist)
        if last is None and r.get("last_price"):
            last = float(r["last_price"])
        since = prices.pct_return(alert_price, last)   # live return since the alert
        rets = {}
        for label, days in WINDOWS:
            if (today - adate).days >= days:
                wp = prices.close_on_or_after(hist, adate + timedelta(days=days))
                rets[label] = prices.pct_return(alert_price, wp)
            else:
                rets[label] = None
        upd = {"last_priced_at": today.isoformat()}
        if alert_price is not None:
            upd["alert_price"] = round(alert_price, 4)
        if last is not None:
            upd["last_price"] = round(last, 4)
        if rets["1W"] is not None:
            upd["ret_1w"] = round(rets["1W"], 2)
        if rets["1M"] is not None:
            upd["ret_1m"] = round(rets["1M"], 2)
        if rets["3M"] is not None:
            upd["ret_3m"] = round(rets["3M"], 2)
        try:
            db.update_performance(r["id"], upd)
        except Exception as exc:  # noqa: BLE001
            log.warning("update_performance failed for %s: %s", ticker, exc)
        results.append({"ticker": ticker, "alert_date": adate,
                        "alert_type": r.get("alert_type"), "since": since, "rets": rets})
        stats.inc("priced")
    return results


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def build_recap(results, today, lookback, max_listed):
    """Return (html_recap, x_text)."""
    n = len(results)
    since_avg = _avg([r["since"] for r in results])
    since_cnt = sum(1 for r in results if r["since"] is not None)
    avgs = {lab: _avg([r["rets"][lab] for r in results]) for lab, _ in WINDOWS}
    counts = {lab: sum(1 for r in results if r["rets"][lab] is not None) for lab, _ in WINDOWS}

    summary = f"Tracking <b>{n} alert{'s' if n != 1 else ''}</b>"
    if since_avg is not None:
        summary += f" · since-alert avg <b>{_fmt_pct(since_avg)}</b>"
    head = [f"📈 <b>Insider Alert Performance</b>",
            f"<i>as of {today.strftime('%b %-d, %Y')}</i>",
            "",
            summary,
            ""]
    for lab, _ in WINDOWS:
        head.append(f"{lab}: <b>{_fmt_pct(avgs[lab])}</b> avg ({counts[lab]})")
    head.append("")
    head.append("<i>Milestone returns fill in as alerts age: 1W after 7 days, "
                "1M after 30, 3M after 90.</i>")
    lines = head + ["", "<b>By alert</b>"]
    shown = sorted(results,
                   key=lambda r: (r["since"] if r["since"] is not None else -1e9),
                   reverse=True)
    for r in shown[:max_listed]:
        parts = []
        if r["since"] is not None:
            parts.append(f"since <b>{_fmt_pct(r['since'])}</b>")
        for lab, _ in WINDOWS:
            v = r["rets"][lab]
            if v is not None:
                parts.append(f"{lab} <b>{_fmt_pct(v)}</b>")
        tail = " · ".join(parts) if parts else "<i>price n/a</i>"
        lines.append(f"• ${escape_html(r['ticker'])} · {r['alert_date'].strftime('%b %-d')} · {tail}")
    if n > max_listed:
        lines.append(f"<i>… +{n - max_listed} more</i>")

    movers = sorted(((r["since"], r["ticker"]) for r in results if r["since"] is not None),
                    reverse=True)
    top = " ".join(f"${t} {v:+.0f}%" for v, t in movers[:3])
    x_lines = [f"📈 Insider-buy alert track record ({n} alerts)"]
    if since_avg is not None:
        x_lines.append(f"Since alert: avg {_fmt_pct(since_avg)} across {since_cnt}")
    seg = " | ".join(f"{lab} avg {_fmt_pct(avgs[lab])}" for lab, _ in WINDOWS if avgs[lab] is not None)
    if seg:
        x_lines.append(seg)
    if top:
        x_lines.append(f"Top: {top}")
    x_lines.append("Public filing data. Not investment advice.")
    return "\n".join(lines), "\n".join(x_lines)


def _post(db, telegram, channel, dedup_key, html, stale_minutes, stats, label):
    if not db.claim_alert(dedup_key, "perf", channel, "recap", {}, stale_minutes):
        stats.inc("skipped_" + label)
        return
    try:
        telegram.send(channel, html)
    except TelegramError as exc:
        db.release_alert(dedup_key)
        log.error("recap post failed (%s): %s", label, exc)
        stats.inc("send_failed")
        return
    db.confirm_alert(dedup_key)
    stats.inc("posted_" + label)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly performance recap")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    stats = RunStats("weekly_recap")

    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key", "telegram_bot_token")

    db = get_db(cfg, dry_run=args.dry_run)
    try:
        db.healthcheck()
    except Exception as exc:  # noqa: BLE001
        log.error("Supabase unreachable (%s) — exiting 1.", exc)
        return 1

    pcfg = cfg.get("performance", {}) or {}
    lookback = pcfg.get("lookback_days", 90)
    max_listed = pcfg.get("max_listed", 40)
    source = prices.make_source(cfg)

    today = now_utc().date()
    since = (today - timedelta(days=lookback)).isoformat()
    rows = db.get_performance_rows(since)
    stats.inc("tracked", len(rows))
    if not rows:
        log.info("No tracked alerts in the last %d days — nothing to recap.", lookback)
        stats.log()
        return 0

    results = price_and_score(db, source, today, rows, stats)
    html, xtext = build_recap(results, today, lookback, max_listed)

    telegram = TelegramClient(
        bot_token=cfg.secrets.telegram_bot_token,
        disclaimer=cfg["delivery"]["disclaimer"],
        max_retries=cfg["delivery"]["telegram_max_retries"],
        backoff_base_sec=cfg["delivery"]["telegram_backoff_base_sec"],
        dry_run=args.dry_run,
    )
    channel = cfg["delivery"]["channels"].get(pcfg.get("recap_channel", "sec_paid"), "")
    stale = cfg.get_path("idempotency.pending_stale_minutes", 30)

    _post(db, telegram, channel, f"perf:recap:{today}", html, stale, stats, "recap")
    xcopy = "📋 <b>Copy for X</b>\n<pre>" + escape_html(xtext) + "</pre>"
    _post(db, telegram, channel, f"perf:xcopy:{today}", xcopy, stale, stats, "xcopy")

    stats.log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
