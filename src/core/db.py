"""Database layer — all state lives in Supabase (Postgres).

Two interchangeable backends behind one API:

* ``SupabaseBackend`` — production. Uses supabase-py. Relies on DB UNIQUE
  constraints for dedupe/idempotency (not just app logic), per the spec.
* ``MemoryBackend``   — in-process dict store, used ONLY for ``--dry-run`` when no
  SUPABASE_URL is configured, so the full pipeline can be exercised offline /
  in CI without a live database. Decision logged in README.

The idempotency contract (``claim_alert`` -> send -> ``confirm_alert`` /
``release_alert``) is what makes every job safe to crash and re-run with zero
duplicate alerts. See README "Idempotency design".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from dateutil import parser as dtparser

log = logging.getLogger("db")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dtparser.parse(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return now_utc()


# ============================================================================
#  Supabase backend
# ============================================================================
class SupabaseBackend:
    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client  # lazy import
        self.client = create_client(url, key)

    # ---- connectivity ------------------------------------------------------
    def healthcheck(self) -> None:
        """Raise if the DB is unreachable (callers exit 1 on Supabase outage)."""
        self.client.table("alerts_sent").select("id").limit(1).execute()

    # ---- low-level helpers -------------------------------------------------
    @staticmethod
    def _is_unique_violation(exc: Exception) -> bool:
        s = str(exc).lower()
        return "23505" in s or "duplicate key" in s or "already exists" in s

    def _insert_ignore(self, table: str, row: dict, returning: bool = False):
        """Insert; treat unique violation as a no-op. Returns True if inserted
        (or the inserted row if returning=True), False/None on conflict."""
        try:
            res = self.client.table(table).insert(row).execute()
            if returning:
                return res.data[0] if res.data else None
            return True
        except Exception as exc:  # noqa: BLE001
            if self._is_unique_violation(exc):
                return None if returning else False
            raise

    # ---- filings -----------------------------------------------------------
    def filing_exists(self, accession: str) -> bool:
        res = (self.client.table("filings").select("id")
               .eq("accession_number", accession).limit(1).execute())
        return bool(res.data)

    def insert_filing(self, record: dict) -> bool:
        return bool(self._insert_ignore("filings", record))

    # ---- insider buys ------------------------------------------------------
    def insider_has_prior_pbuy(self, insider_cik: str, exclude_accession: str = "") -> bool:
        q = (self.client.table("insider_buys").select("accession_number")
             .eq("insider_cik", insider_cik).eq("txn_code", "P").limit(2))
        res = q.execute()
        rows = res.data or []
        for r in rows:
            if r.get("accession_number") != exclude_accession:
                return True
        return False

    def insert_buy(self, record: dict) -> bool:
        return bool(self._insert_ignore("insider_buys", record))

    def get_window_buys(self, ticker: str, since_date: str) -> list[dict]:
        res = (self.client.table("insider_buys").select("*")
               .eq("ticker", ticker).eq("txn_code", "P")
               .gte("txn_date", since_date).execute())
        return res.data or []

    # ---- clusters ----------------------------------------------------------
    def get_active_cluster(self, ticker: str) -> Optional[dict]:
        res = (self.client.table("clusters").select("*")
               .eq("ticker", ticker).eq("status", "active").limit(1).execute())
        return res.data[0] if res.data else None

    def create_cluster(self, record: dict) -> Optional[dict]:
        return self._insert_ignore("clusters", record, returning=True)

    def update_cluster(self, cluster_id: Any, fields: dict) -> None:
        fields = {**fields, "updated_at": now_utc().isoformat()}
        self.client.table("clusters").update(fields).eq("id", cluster_id).execute()

    def get_active_clusters(self) -> list[dict]:
        res = (self.client.table("clusters").select("*")
               .eq("status", "active").execute())
        return res.data or []

    # ---- catalysts ---------------------------------------------------------
    def upsert_catalyst(self, record: dict) -> tuple[Optional[dict], bool]:
        inserted = self._insert_ignore("catalysts", record, returning=True)
        if inserted is not None:
            return inserted, True
        # Conflict on natural key -> fetch existing (no clobber of manual edits).
        q = (self.client.table("catalysts").select("*")
             .eq("ticker", record["ticker"])
             .eq("catalyst_type", record["catalyst_type"]))
        if record.get("expected_date"):
            q = q.eq("expected_date", record["expected_date"])
        res = q.limit(1).execute()
        return (res.data[0] if res.data else None), False

    def get_upcoming_catalysts(self) -> list[dict]:
        res = (self.client.table("catalysts").select("*")
               .eq("status", "upcoming").execute())
        return res.data or []

    def get_catalysts_for_ticker_within(self, ticker: str, within_days: int) -> list[dict]:
        cutoff = (now_utc().date() + timedelta(days=within_days)).isoformat()
        today = now_utc().date().isoformat()
        res = (self.client.table("catalysts").select("*")
               .eq("ticker", ticker).eq("status", "upcoming")
               .gte("expected_date", today).lte("expected_date", cutoff).execute())
        return res.data or []

    def update_catalyst_status(self, catalyst_id: Any, status: str) -> None:
        self.client.table("catalysts").update(
            {"status": status, "updated_at": now_utc().isoformat()}
        ).eq("id", catalyst_id).execute()

    def get_tracked_tickers(self) -> set[str]:
        res = self.client.table("catalysts").select("ticker").execute()
        return {r["ticker"] for r in (res.data or []) if r.get("ticker")}

    # ---- clinicaltrials.gov ------------------------------------------------
    def get_ct_trial(self, nct_id: str) -> Optional[dict]:
        res = (self.client.table("ct_trials").select("*")
               .eq("nct_id", nct_id).limit(1).execute())
        return res.data[0] if res.data else None

    def upsert_ct_trial(self, record: dict) -> None:
        existing = self.get_ct_trial(record["nct_id"])
        if existing:
            self.client.table("ct_trials").update(
                {**record, "updated_at": now_utc().isoformat()}
            ).eq("nct_id", record["nct_id"]).execute()
        else:
            self._insert_ignore("ct_trials", record)

    # ---- press seen --------------------------------------------------------
    def press_seen(self, guid: str) -> bool:
        res = (self.client.table("press_seen").select("id")
               .eq("guid", guid).limit(1).execute())
        return bool(res.data)

    def mark_press_seen(self, record: dict) -> None:
        self._insert_ignore("press_seen", record)

    # ---- alerts_sent (idempotency ledger) ----------------------------------
    def claim_alert(self, dedup_key: str, feed: str, channel: str,
                    alert_type: str, payload: dict, stale_minutes: int) -> bool:
        row = {"dedup_key": dedup_key, "feed": feed, "channel": channel,
               "alert_type": alert_type, "status": "pending", "payload": payload}
        if self._insert_ignore("alerts_sent", row):
            return True
        # Conflict: inspect existing.
        res = (self.client.table("alerts_sent").select("*")
               .eq("dedup_key", dedup_key).limit(1).execute())
        if not res.data:
            return False
        existing = res.data[0]
        if existing.get("status") == "sent":
            return False
        # pending: reclaim if crash-orphaned (older than stale window).
        age = now_utc() - _parse_ts(existing.get("created_at"))
        if age > timedelta(minutes=stale_minutes):
            self.client.table("alerts_sent").update(
                {"created_at": now_utc().isoformat(), "channel": channel}
            ).eq("dedup_key", dedup_key).execute()
            return True
        return False

    def confirm_alert(self, dedup_key: str) -> None:
        self.client.table("alerts_sent").update(
            {"status": "sent", "sent_at": now_utc().isoformat()}
        ).eq("dedup_key", dedup_key).execute()

    def release_alert(self, dedup_key: str) -> None:
        """Delete an unconfirmed claim so a later run retries (send failed)."""
        self.client.table("alerts_sent").delete().eq("dedup_key", dedup_key)\
            .eq("status", "pending").execute()

    # ---- delayed queue -----------------------------------------------------
    def enqueue_delayed(self, dedup_key: str, feed: str, free_channel: str,
                        html_text: str, available_at: datetime) -> bool:
        row = {"dedup_key": dedup_key, "feed": feed, "free_channel": free_channel,
               "html": html_text, "available_at": available_at.isoformat()}
        return bool(self._insert_ignore("delayed_queue", row))

    def get_due_delayed(self) -> list[dict]:
        res = (self.client.table("delayed_queue").select("*")
               .eq("released", False)
               .lte("available_at", now_utc().isoformat()).execute())
        return res.data or []

    def mark_delayed_released(self, row_id: Any) -> None:
        self.client.table("delayed_queue").update(
            {"released": True, "released_at": now_utc().isoformat()}
        ).eq("id", row_id).execute()

    # ---- alert performance ------------------------------------------------
    def record_alert_performance(self, dedup_key: str, feed: str, alert_type: str,
                                 ticker: str, alert_date) -> bool:
        row = {"dedup_key": dedup_key, "feed": feed, "alert_type": alert_type,
               "ticker": ticker,
               "alert_date": alert_date.isoformat() if hasattr(alert_date, "isoformat") else str(alert_date)}
        return bool(self._insert_ignore("alert_performance", row))

    def get_performance_rows(self, since_date: str) -> list[dict]:
        res = (self.client.table("alert_performance").select("*")
               .gte("alert_date", since_date).execute())
        return res.data or []

    def update_performance(self, perf_id: Any, fields: dict) -> None:
        fields = {**fields, "updated_at": now_utc().isoformat()}
        self.client.table("alert_performance").update(fields).eq("id", perf_id).execute()


# ============================================================================
#  In-memory backend (dry-run / offline only)
# ============================================================================
class MemoryBackend:
    def __init__(self) -> None:
        self.filings: dict[str, dict] = {}
        self.buys: dict[str, dict] = {}          # accession -> row
        self.clusters: list[dict] = []
        self.catalysts: list[dict] = []
        self.ct: dict[str, dict] = {}
        self.press: set[str] = set()
        self.alerts: dict[str, dict] = {}
        self.delayed: list[dict] = []
        self.performance: dict[str, dict] = {}
        self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def healthcheck(self) -> None:
        return None

    # filings
    def filing_exists(self, accession: str) -> bool:
        return accession in self.filings

    def insert_filing(self, record: dict) -> bool:
        if record["accession_number"] in self.filings:
            return False
        self.filings[record["accession_number"]] = dict(record)
        return True

    # buys
    def insider_has_prior_pbuy(self, insider_cik: str, exclude_accession: str = "") -> bool:
        for acc, r in self.buys.items():
            if r.get("insider_cik") == insider_cik and r.get("txn_code") == "P" \
                    and acc != exclude_accession:
                return True
        return False

    def insert_buy(self, record: dict) -> bool:
        if record["accession_number"] in self.buys:
            return False
        self.buys[record["accession_number"]] = dict(record)
        return True

    def get_window_buys(self, ticker: str, since_date: str) -> list[dict]:
        return [dict(r) for r in self.buys.values()
                if r.get("ticker") == ticker and r.get("txn_code") == "P"
                and str(r.get("txn_date")) >= since_date]

    # clusters
    def get_active_cluster(self, ticker: str) -> Optional[dict]:
        for c in self.clusters:
            if c["ticker"] == ticker and c["status"] == "active":
                return dict(c)
        return None

    def create_cluster(self, record: dict) -> dict:
        row = {"id": self._next_id(), **record}
        self.clusters.append(row)
        return dict(row)

    def update_cluster(self, cluster_id: Any, fields: dict) -> None:
        for c in self.clusters:
            if c["id"] == cluster_id:
                c.update(fields)
                return

    def get_active_clusters(self) -> list[dict]:
        return [dict(c) for c in self.clusters if c.get("status") == "active"]

    # catalysts
    def _catalyst_key(self, r: dict):
        return (r.get("ticker"), r.get("catalyst_type"),
                r.get("drug") or "", str(r.get("expected_date") or "1900-01-01"))

    def upsert_catalyst(self, record: dict) -> tuple[Optional[dict], bool]:
        key = self._catalyst_key(record)
        for c in self.catalysts:
            if self._catalyst_key(c) == key:
                return dict(c), False
        row = {"id": self._next_id(), **record}
        self.catalysts.append(row)
        return dict(row), True

    def get_upcoming_catalysts(self) -> list[dict]:
        return [dict(c) for c in self.catalysts if c.get("status") == "upcoming"]

    def get_catalysts_for_ticker_within(self, ticker: str, within_days: int) -> list[dict]:
        today = now_utc().date()
        cutoff = today + timedelta(days=within_days)
        out = []
        for c in self.catalysts:
            if c.get("ticker") != ticker or c.get("status") != "upcoming":
                continue
            ed = c.get("expected_date")
            if not ed:
                continue
            try:
                d = dtparser.parse(str(ed)).date()
            except Exception:  # noqa: BLE001
                continue
            if today <= d <= cutoff:
                out.append(dict(c))
        return out

    def update_catalyst_status(self, catalyst_id: Any, status: str) -> None:
        for c in self.catalysts:
            if c["id"] == catalyst_id:
                c["status"] = status
                return

    def get_tracked_tickers(self) -> set[str]:
        return {c["ticker"] for c in self.catalysts if c.get("ticker")}

    # ct
    def get_ct_trial(self, nct_id: str) -> Optional[dict]:
        return dict(self.ct[nct_id]) if nct_id in self.ct else None

    def upsert_ct_trial(self, record: dict) -> None:
        self.ct[record["nct_id"]] = dict(record)

    # press
    def press_seen(self, guid: str) -> bool:
        return guid in self.press

    def mark_press_seen(self, record: dict) -> None:
        self.press.add(record["guid"])

    # alerts
    def claim_alert(self, dedup_key: str, feed: str, channel: str,
                    alert_type: str, payload: dict, stale_minutes: int) -> bool:
        existing = self.alerts.get(dedup_key)
        if existing is None:
            self.alerts[dedup_key] = {"status": "pending", "created_at": now_utc(),
                                      "feed": feed, "channel": channel,
                                      "alert_type": alert_type}
            return True
        if existing["status"] == "sent":
            return False
        if now_utc() - existing["created_at"] > timedelta(minutes=stale_minutes):
            existing["created_at"] = now_utc()
            return True
        return False

    def confirm_alert(self, dedup_key: str) -> None:
        if dedup_key in self.alerts:
            self.alerts[dedup_key]["status"] = "sent"

    def release_alert(self, dedup_key: str) -> None:
        self.alerts.pop(dedup_key, None)

    # delayed
    def enqueue_delayed(self, dedup_key: str, feed: str, free_channel: str,
                        html_text: str, available_at: datetime) -> bool:
        if any(d["dedup_key"] == dedup_key for d in self.delayed):
            return False
        self.delayed.append({"id": self._next_id(), "dedup_key": dedup_key,
                             "feed": feed, "free_channel": free_channel,
                             "html": html_text, "available_at": available_at,
                             "released": False})
        return True

    def get_due_delayed(self) -> list[dict]:
        now = now_utc()
        return [dict(d) for d in self.delayed
                if not d["released"] and d["available_at"] <= now]

    def mark_delayed_released(self, row_id: Any) -> None:
        for d in self.delayed:
            if d["id"] == row_id:
                d["released"] = True

    # ---- alert performance ------------------------------------------------
    def record_alert_performance(self, dedup_key: str, feed: str, alert_type: str,
                                 ticker: str, alert_date) -> bool:
        if dedup_key in self.performance:
            return False
        self.performance[dedup_key] = {"id": self._next_id(), "dedup_key": dedup_key,
                                       "feed": feed, "alert_type": alert_type,
                                       "ticker": ticker, "alert_date": str(alert_date)}
        return True

    def get_performance_rows(self, since_date: str) -> list[dict]:
        return [dict(r) for r in self.performance.values()
                if str(r["alert_date"]) >= str(since_date)]

    def update_performance(self, perf_id, fields: dict) -> None:
        for r in self.performance.values():
            if r["id"] == perf_id:
                r.update(fields)
                return


# ============================================================================
#  Factory
# ============================================================================
def get_db(cfg, dry_run: bool = False):
    """Return a backend. Uses Supabase when configured; falls back to an in-memory
    store ONLY for dry-run with no SUPABASE_URL (offline testing)."""
    url = cfg.secrets.supabase_url
    key = cfg.secrets.supabase_key
    if url and key:
        log.info("Using Supabase backend.")
        return SupabaseBackend(url, key)
    if dry_run:
        log.warning("No SUPABASE_URL set; using in-memory backend (dry-run only).")
        return MemoryBackend()
    raise RuntimeError(
        "SUPABASE_URL / SUPABASE_KEY are required for live runs. "
        "Set them (see .env.example) or pass --dry-run for offline testing."
    )
