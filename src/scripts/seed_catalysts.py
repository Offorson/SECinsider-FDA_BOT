"""Manual catalyst seeding from CSV.

Loads the initial 100-200 known PDUFA dates etc. Validates every row, reports
rejects with reasons, and NEVER crashes on a bad row. Idempotent: re-running the
same CSV updates nothing it already has (natural-key dedupe in the DB).

    python -m src.scripts.seed_catalysts catalysts_seed.csv
    python -m src.scripts.seed_catalysts catalysts_seed.csv --dry-run

CSV columns (header row required):
    ticker, company, drug, catalyst_type, expected_date, date_precision,
    source_url, status, notes

expected_date accepts 2026-08-15 (day), 2026-08 (month), "Q3 2026" (quarter),
or "August 2026". date_precision is inferred when blank.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import date, datetime
from typing import Optional

from dateutil import parser as dtparser

from src.core.config import load_config
from src.core.db import get_db
from src.core.alerting import RunStats, setup_logging

log = logging.getLogger("seed")

VALID_TYPES = {"PDUFA", "AdCom", "Phase 1", "Phase 2", "Phase 3",
               "CRL", "Approval", "Readout"}
_QUARTER_RE = re.compile(r"(?:Q([1-4])[\s\-]*(\d{4}))|(?:(\d{4})[\s\-]*Q([1-4]))",
                         re.IGNORECASE)


def parse_expected_date(raw: str, given_precision: str = "") -> tuple[Optional[str], str]:
    """Return (iso_date_or_None, precision). Quarter/month stored as first day."""
    raw = (raw or "").strip()
    if not raw:
        return None, given_precision or "day"
    qm = _QUARTER_RE.search(raw)
    if qm:
        q = int(qm.group(1) or qm.group(4))
        year = int(qm.group(2) or qm.group(3))
        month = (q - 1) * 3 + 1
        return date(year, month, 1).isoformat(), "quarter"
    # YYYY-MM exactly
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        y, m = raw.split("-")
        return date(int(y), int(m), 1).isoformat(), "month"
    # YYYY-MM-DD exactly
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw, given_precision or "day"
    # Flexible: "August 2026", "Aug 15 2026", etc.
    try:
        dt = dtparser.parse(raw, default=datetime(2000, 1, 1))
        # If no day token present, treat as month precision.
        has_day = bool(re.search(r"\b\d{1,2}\b", raw.replace(str(dt.year), "")))
        if has_day:
            return dt.date().isoformat(), given_precision or "day"
        return date(dt.year, dt.month, 1).isoformat(), "month"
    except (ValueError, OverflowError):
        raise ValueError(f"unparseable date: {raw!r}")


def validate_row(row: dict) -> tuple[Optional[dict], Optional[str]]:
    ticker = (row.get("ticker") or "").strip().upper()
    ctype = (row.get("catalyst_type") or "").strip()
    if not ticker:
        return None, "missing ticker"
    if not ctype:
        return None, "missing catalyst_type"
    if ctype not in VALID_TYPES:
        return None, f"invalid catalyst_type {ctype!r} (allowed: {sorted(VALID_TYPES)})"
    try:
        exp_date, precision = parse_expected_date(
            row.get("expected_date", ""), (row.get("date_precision") or "").strip())
    except ValueError as exc:
        return None, str(exc)
    status = (row.get("status") or "upcoming").strip() or "upcoming"
    if status not in {"upcoming", "hit", "passed-unresolved"}:
        return None, f"invalid status {status!r}"
    return {
        "ticker": ticker,
        "company": (row.get("company") or "").strip(),
        "drug": (row.get("drug") or "").strip(),
        "catalyst_type": ctype,
        "expected_date": exp_date,
        "date_precision": precision,
        "source_url": (row.get("source_url") or "").strip(),
        "status": status,
        "needs_review": False,
        "notes": (row.get("notes") or "").strip(),
    }, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Seed catalysts from CSV")
    ap.add_argument("csv_path")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate + report without writing to the DB")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    if not args.dry_run:
        cfg.secrets.require("supabase_url", "supabase_key")
    db = get_db(cfg, dry_run=args.dry_run)
    stats = RunStats("seed_catalysts")

    try:
        fh = open(args.csv_path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        log.error("Cannot open %s: %s", args.csv_path, exc)
        return 1

    rejects: list[tuple[int, str]] = []
    with fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):  # row 1 = header
            record, err = validate_row(row)
            if err:
                rejects.append((i, err))
                stats.inc("rejected")
                continue
            try:
                _, created = db.upsert_catalyst(record)
            except Exception as exc:  # noqa: BLE001 — never crash on one bad row
                rejects.append((i, f"db error: {exc}"))
                stats.inc("rejected")
                continue
            stats.inc("inserted" if created else "duplicate")

    stats.log()
    if rejects:
        log.warning("%d rejected row(s):", len(rejects))
        for line_no, reason in rejects:
            log.warning("  row %d: %s", line_no, reason)
    if args.dry_run:
        log.info("Dry-run: no rows written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
