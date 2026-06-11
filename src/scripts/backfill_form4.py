"""One-time setup: warm the cluster window + first-time-buyer history.

Pulls the last N days of Form 4 filings from EDGAR's daily *master* index
(pipe-delimited, an official index file -- not HTML scraping), parses each, and
stores filings + qualifying code-P buys. It does NOT send alerts (we never want a
flood of stale cluster alerts on first launch). It instead records any already-
existing clusters as 'active' and pre-acknowledged, so the first live poll only
fires UPGRADES when genuinely new insiders appear.

    python -m src.scripts.backfill_form4 --days 7 --max-filings 6000

Hits live EDGAR, so SEC_CONTACT_EMAIL and SUPABASE_* must be set. Respects the
<=9 req/s limit. Resilient: a single bad filing is skipped (logged), never fatal,
and it is safe to re-run (already-stored filings are skipped).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from src.core.config import load_config
from src.core.db import get_db, now_utc
from src.core.alerting import RunStats, setup_logging
from src.feeds.sec_form4 import (EdgarSource, FeedEntry, EdgarUnavailable,
                                 parse_form4_xml, build_buy_record, SecForm4Feed)

log = logging.getLogger("backfill")

ARCHIVES = "https://www.sec.gov/Archives"


def daily_master_url(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return (f"{ARCHIVES}/edgar/daily-index/{d.year}/QTR{q}/"
            f"master.{d.strftime('%Y%m%d')}.idx")


def parse_master_index(text: str) -> list[tuple[str, str]]:
    """Return [(cik, accession)] for Form 4 rows. master.idx is pipe-delimited:
    CIK|Company Name|Form Type|Date Filed|File Name"""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, _company, form_type, _filed, filename = parts
        if form_type.strip() != "4":
            continue
        base = filename.rsplit("/", 1)[-1]
        accession = base[:-4] if base.endswith(".txt") else base
        rows.append((cik.strip(), accession.strip()))
    return rows


def _process_filing(db, source, cik, accession, d, weights, min_value, stats):
    """Process one filing. Returns the ticker if a qualifying buy was stored,
    else None. Raises only on programmer error; transient/data errors are handled
    by the caller's try/except so one filing can never kill the whole run."""
    entry = FeedEntry(accession=accession, cik=cik,
                      index_url=f"{ARCHIVES}/edgar/data/{cik}/"
                                f"{accession.replace('-', '')}/")
    try:
        xml = source.get_filing_xml(entry)
    except (EdgarUnavailable, FileNotFoundError):
        stats.inc("xml_missing")
        return None
    if not xml:
        stats.inc("xml_missing")
        return None
    parsed = parse_form4_xml(xml)
    if parsed is None or not parsed.ticker:
        stats.inc("parse_failed")
        return None
    stats.inc("parsed")
    first_time = not db.insider_has_prior_pbuy(parsed.insider_cik,
                                               exclude_accession=accession)
    db.insert_filing({
        "accession_number": accession, "form_type": "4",
        "issuer_cik": parsed.issuer_cik, "ticker": parsed.ticker,
        "issuer_name": parsed.issuer_name,
        "filed_at": d.isoformat(), "source_url": entry.index_url,
        "has_qualifying_buy": False,
    })
    buy = build_buy_record(parsed, accession, entry.index_url, weights, first_time)
    # txn_date is NOT NULL in the DB; skip a qualifying buy that lacks a parseable
    # date (it could not be placed in a time window anyway).
    if buy and buy.get("txn_date") and (buy["total_value"] or 0) >= min_value:
        db.insert_buy(buy)
        stats.inc("qualifying_buys")
        return buy["ticker"]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill Form 4 history")
    ap.add_argument("--days", type=int, default=None,
                    help="how many days back to pull (default: config backfill.default_days)")
    ap.add_argument("--max-filings", type=int, default=20000,
                    help="safety cap on filings processed")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    # Keep third-party HTTP libraries quiet even in --verbose so logs stay usable.
    for noisy in ("httpx", "httpcore", "hpack", "h2", "urllib3", "hpack.hpack",
                  "hpack.table"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config()
    cfg.secrets.require("supabase_url", "supabase_key")
    if not cfg.secrets.sec_contact_email:
        log.error("SEC_CONTACT_EMAIL is required to query EDGAR.")
        return 1

    days = args.days or cfg.get_path("backfill.default_days", 90)
    db = get_db(cfg, dry_run=False)
    try:
        db.healthcheck()
    except Exception as exc:  # noqa: BLE001
        log.error("Supabase unreachable (%s) -- exiting 1.", exc)
        return 1

    source = EdgarSource(cfg, user_agent=cfg.secrets.sec_user_agent)
    feed = SecForm4Feed(cfg)
    weights = cfg["sec"]["role_weights"]
    min_value = cfg["sec"].get("min_total_value", 25000)
    stats = RunStats("backfill")

    touched: set[str] = set()
    today = now_utc().date()
    processed = 0
    capped = False

    # newest day first, so a time-limited / capped run keeps the most
    # recent (most relevant to the current cluster window) filings.
    for offset in range(1, days + 1):
        if capped:
            break
        d = today - timedelta(days=offset)
        try:
            text = source._get(daily_master_url(d)).decode("utf-8", "replace")
        except FileNotFoundError:
            continue  # weekend / holiday -- no index that day
        except EdgarUnavailable as exc:
            log.warning("EDGAR unavailable for %s (%s) -- stopping.", d, exc)
            break

        rows = parse_master_index(text)
        log.info("%s: %d Form 4 filings", d.isoformat(), len(rows))
        for cik, accession in rows:
            if processed >= args.max_filings:
                log.info("Reached --max-filings cap (%d).", args.max_filings)
                capped = True
                break
            try:
                if db.filing_exists(accession):
                    stats.inc("skipped_seen")
                    continue
                processed += 1
                ticker = _process_filing(db, source, cik, accession, d,
                                         weights, min_value, stats)
                if ticker:
                    touched.add(ticker)
            except Exception as exc:  # noqa: BLE001 -- one filing must never be fatal
                stats.inc("errors")
                log.warning("Skipping %s after error: %s", accession, exc)
                continue
        if processed and processed % 250 == 0:
            log.info("...progress: processed=%d", processed)

    # Seed existing clusters silently so future polls fire upgrades, not floods.
    try:
        seeded = _seed_existing_clusters(db, feed, touched)
        stats.inc("clusters_seeded", seeded)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cluster seeding skipped after error: %s", exc)

    stats.log()
    log.info("Backfill complete. Processed %d filings.", processed)
    return 0


def _seed_existing_clusters(db, feed: SecForm4Feed, tickers: set[str]) -> int:
    seeded = 0
    since = (now_utc().date() - timedelta(days=feed.window_days)).isoformat()
    for ticker in tickers:
        try:
            buys = db.get_window_buys(ticker, since)
            ciks = sorted({b["insider_cik"] for b in buys})
            if len(ciks) < feed.min_insiders:
                continue
            if db.get_active_cluster(ticker):
                continue
            dates = [b.get("txn_date") for b in buys if b.get("txn_date")]
            db.create_cluster({
                "ticker": ticker,
                "issuer_name": next((b.get("issuer_name") for b in buys if b.get("issuer_name")), ""),
                "member_ciks": ciks, "member_count": len(ciks),
                "alerted_count": len(ciks), "window_days": feed.window_days,
                "first_buy_date": min(dates) if dates else None,
                "last_buy_date": max(dates) if dates else None,
                "combined_value": sum((b.get("total_value") or 0) for b in buys),
                "status": "active",
            })
            seeded += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Cluster seed for %s skipped: %s", ticker, exc)
    return seeded


if __name__ == "__main__":
    sys.exit(main())
