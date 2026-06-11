"""SEC Insider Cluster Bot — Module 1.

EDGAR polling + Form 4 parsing + signal scoring + cluster detection.

HARD RULES enforced here (per spec / SEC fair-access):
* Every request carries a descriptive ``User-Agent`` ("AppName email").
* A token-bucket rate limiter keeps us at/under ``rate_limit_per_sec`` (<=9).
* JSON/Atom/XML endpoints only — we never parse EDGAR HTML pages.
* 429/403 -> exponential backoff; persistent failure -> ``EdgarUnavailable`` so
  the job can exit 0 (upstream outage, not our bug).

The feed source is abstracted (``EdgarSource`` vs ``FixtureSource``) so the whole
pipeline can run offline against local fixtures for tests/dry-run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import feedparser
import requests
from lxml import etree

from src.core.db import now_utc

log = logging.getLogger("sec")


class EdgarUnavailable(Exception):
    """Raised on upstream EDGAR outage so the job exits 0 (no false failure)."""


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    """Simple spacing limiter: ensures >= 1/rate seconds between calls."""

    def __init__(self, per_sec: float) -> None:
        self.min_interval = 1.0 / max(per_sec, 0.1)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@dataclass
class FeedEntry:
    accession: str
    cik: str
    title: str = ""
    index_url: str = ""
    fixture_file: str = ""


class EdgarSource:
    """Live EDGAR source. Uses the latest-Form-4 Atom feed + per-filing index.json
    to locate the ownership XML (no HTML scraping)."""

    ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

    def __init__(self, cfg, user_agent: str) -> None:
        self.cfg = cfg
        self.feed_url = cfg["sec"]["feed_url"]
        self.timeout = cfg["sec"].get("request_timeout_sec", 20)
        self.limiter = RateLimiter(cfg["sec"].get("rate_limit_per_sec", 9))
        self.headers = {"User-Agent": user_agent,
                        "Accept-Encoding": "gzip, deflate",
                        "Host": "www.sec.gov"}
        self.session = requests.Session()

    def _get(self, url: str, max_retries: int = 4) -> bytes:
        headers = dict(self.headers)
        # Host header must match the URL's host.
        m = re.match(r"https?://([^/]+)/", url)
        if m:
            headers["Host"] = m.group(1)
        for attempt in range(1, max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == max_retries:
                    raise EdgarUnavailable(f"network error: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 404:
                raise FileNotFoundError(url)
            if resp.status_code in (429, 403):
                log.warning("EDGAR %s on %s (attempt %d) — backing off",
                            resp.status_code, url, attempt)
                time.sleep(2 ** attempt)
                continue
            if 500 <= resp.status_code < 600:
                if attempt == max_retries:
                    raise EdgarUnavailable(f"HTTP {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            raise EdgarUnavailable(f"HTTP {resp.status_code} on {url}")
        raise EdgarUnavailable(f"exhausted retries on {url}")

    def get_feed_entries(self) -> list[FeedEntry]:
        raw = self._get(self.feed_url)
        parsed = feedparser.parse(raw)
        entries: list[FeedEntry] = []
        for e in parsed.entries:
            link = e.get("link", "")
            acc, cik = _accession_cik_from_link(link)
            if not acc:
                # Some feeds carry accession in the id/title.
                acc, cik = _accession_cik_from_link(e.get("id", ""))
            if not acc:
                continue
            entries.append(FeedEntry(accession=acc, cik=cik,
                                     title=e.get("title", ""), index_url=link))
        return entries

    def get_filing_xml(self, entry: FeedEntry) -> Optional[bytes]:
        cik = str(int(entry.cik)) if entry.cik.isdigit() else entry.cik
        acc_nodash = entry.accession.replace("-", "")
        index_url = f"{self.ARCHIVES}/{cik}/{acc_nodash}/index.json"
        try:
            idx = json.loads(self._get(index_url))
        except FileNotFoundError:
            log.debug("index.json missing for %s", entry.accession)
            return None
        items = idx.get("directory", {}).get("item", [])
        xml_names = [it["name"] for it in items
                     if it.get("name", "").lower().endswith(".xml")]
        # Prefer obvious ownership docs; fall back to any xml that parses.
        xml_names.sort(key=lambda n: (0 if "form4" in n.lower()
                                      or "ownership" in n.lower()
                                      or "primary_doc" in n.lower() else 1, n))
        for name in xml_names:
            doc_url = f"{self.ARCHIVES}/{cik}/{acc_nodash}/{name}"
            try:
                content = self._get(doc_url)
            except FileNotFoundError:
                continue
            if b"ownershipDocument" in content:
                return content
        return None


class FixtureSource:
    """Offline source backed by local fixture files (tests/dry-run)."""

    def __init__(self, fixture_dir: str) -> None:
        from pathlib import Path
        self.dir = Path(fixture_dir)
        self.manifest = json.loads((self.dir / "feed.json").read_text())

    def get_feed_entries(self) -> list[FeedEntry]:
        return [FeedEntry(accession=m["accession"], cik=str(m["cik"]),
                          title=m.get("title", ""),
                          index_url=m.get("index_url", ""),
                          fixture_file=m["file"]) for m in self.manifest]

    def get_filing_xml(self, entry: FeedEntry) -> Optional[bytes]:
        from pathlib import Path
        p = Path(self.dir) / entry.fixture_file
        return p.read_bytes() if p.exists() else None


def _accession_cik_from_link(link: str) -> tuple[str, str]:
    """Extract (accession, cik) from an EDGAR archives URL."""
    if not link:
        return "", ""
    cik = ""
    m_cik = re.search(r"/data/(\d+)/", link)
    if m_cik:
        cik = m_cik.group(1)
    m_acc = re.search(r"(\d{10}-\d{2}-\d{6})", link)
    if m_acc:
        return m_acc.group(1), cik
    m_acc2 = re.search(r"/(\d{18})[/-]", link)
    if m_acc2:
        d = m_acc2.group(1)
        return f"{d[:10]}-{d[10:12]}-{d[12:]}", cik
    return "", cik


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------
@dataclass
class Transaction:
    code: str
    acquired_disposed: str
    txn_date: Optional[str]
    shares: float
    price: Optional[float]
    owned_following: Optional[float]
    order: int


@dataclass
class ParsedForm4:
    issuer_cik: str = ""
    issuer_name: str = ""
    ticker: str = ""
    insider_cik: str = ""
    insider_name: str = ""
    is_director: bool = False
    is_officer: bool = False
    is_ten_pct: bool = False
    is_other: bool = False
    officer_title: str = ""
    transactions: list[Transaction] = field(default_factory=list)


def _txt(node, path: str) -> str:
    el = node.find(path)
    if el is None:
        return ""
    return (el.text or "").strip()


def _val(node, path: str) -> str:
    """Read <path><value>X</value></path> or <path>X</path>."""
    el = node.find(path)
    if el is None:
        return ""
    v = el.find("value")
    if v is not None:
        return (v.text or "").strip()
    return (el.text or "").strip()


def _to_float(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_form4_xml(xml_bytes: bytes) -> Optional[ParsedForm4]:
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        log.debug("XML parse error: %s", exc)
        return None
    if root.tag != "ownershipDocument":
        # strip namespace if present
        if not root.tag.endswith("ownershipDocument"):
            return None

    p = ParsedForm4()
    issuer = root.find("issuer")
    if issuer is not None:
        p.issuer_cik = _txt(issuer, "issuerCik")
        p.issuer_name = _txt(issuer, "issuerName")
        p.ticker = _txt(issuer, "issuerTradingSymbol").upper()

    owner = root.find("reportingOwner")
    if owner is not None:
        p.insider_cik = _txt(owner, "reportingOwnerId/rptOwnerCik")
        p.insider_name = _txt(owner, "reportingOwnerId/rptOwnerName")
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            p.is_director = _txt(rel, "isDirector") in ("1", "true")
            p.is_officer = _txt(rel, "isOfficer") in ("1", "true")
            p.is_ten_pct = _txt(rel, "isTenPercentOwner") in ("1", "true")
            p.is_other = _txt(rel, "isOther") in ("1", "true")
            p.officer_title = _txt(rel, "officerTitle")

    order = 0
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for tx in nd_table.findall("nonDerivativeTransaction"):
            coding = tx.find("transactionCoding")
            code = _txt(coding, "transactionCode") if coding is not None else ""
            amounts = tx.find("transactionAmounts")
            ad = _val(amounts, "transactionAcquiredDisposedCode") if amounts is not None else ""
            shares = _to_float(_val(amounts, "transactionShares")) if amounts is not None else None
            price = _to_float(_val(amounts, "transactionPricePerShare")) if amounts is not None else None
            post = tx.find("postTransactionAmounts")
            owned = _to_float(_val(post, "sharesOwnedFollowingTransaction")) if post is not None else None
            tdate = _val(tx, "transactionDate")
            p.transactions.append(Transaction(
                code=code, acquired_disposed=ad, txn_date=tdate or None,
                shares=shares or 0.0, price=price, owned_following=owned, order=order))
            order += 1
    return p


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def compute_role(parsed: ParsedForm4, weights: dict) -> tuple[float, str]:
    """Return (role_weight, human role summary). Multiple roles -> max weight."""
    title = (parsed.officer_title or "").lower()
    candidates: list[tuple[float, str]] = []

    if parsed.is_officer:
        if re.search(r"\bce[o0]\b|chief executive", title):
            candidates.append((weights["ceo"], "CEO"))
        elif re.search(r"\bcfo\b|chief financial", title):
            candidates.append((weights["cfo"], "CFO"))
        elif re.search(r"president", title):
            candidates.append((weights["president"], "President"))
        elif re.search(r"\bcoo\b|chief operating", title):
            candidates.append((weights["coo"], "COO"))
        elif re.search(r"chief|\bct[o0]\b|\bcmo\b|\bclo\b|\bcso\b|\bcao\b|\bcco\b", title):
            candidates.append((weights["other_c_suite"], parsed.officer_title or "Officer"))
        else:
            candidates.append((weights["other_c_suite"], parsed.officer_title or "Officer"))
    if parsed.is_director:
        candidates.append((weights["director"], "Director"))
    if parsed.is_ten_pct:
        candidates.append((weights["ten_percent_owner"], "10% owner"))
    if parsed.is_other and not candidates:
        candidates.append((1.0, "Insider"))

    if not candidates:
        return 1.0, "Insider"
    best_weight = max(c[0] for c in candidates)
    # role summary lists all roles, primary first
    labels = [c[1] for c in sorted(candidates, key=lambda c: -c[0])]
    # de-dup while preserving order
    seen, uniq = set(), []
    for l in labels:
        if l not in seen:
            uniq.append(l); seen.add(l)
    return best_weight, ", ".join(uniq)


def build_buy_record(parsed: ParsedForm4, accession: str, source_url: str,
                     weights: dict, first_time: bool) -> Optional[dict]:
    """Aggregate qualifying code-P transactions into one buy record. Returns None
    if the filing has no qualifying open-market purchase."""
    p_txns = [t for t in parsed.transactions
              if t.code == "P" and t.acquired_disposed == "A"]
    if not p_txns:
        return None

    total_shares = sum(t.shares for t in p_txns)
    total_value = sum(t.shares * t.price for t in p_txns if t.price is not None)
    vwap = (total_value / total_shares) if total_shares and total_value else (
        p_txns[0].price)

    # owned-before from the earliest transaction overall, owned-after from latest.
    all_txns = sorted(parsed.transactions, key=lambda t: (t.txn_date or "", t.order))
    owned_after = None
    for t in reversed(all_txns):
        if t.owned_following is not None:
            owned_after = t.owned_following
            break
    owned_before = None
    for t in all_txns:
        if t.owned_following is not None:
            signed = t.shares if t.acquired_disposed == "A" else -t.shares
            owned_before = t.owned_following - signed
            break

    new_stake = owned_before is not None and owned_before <= 0
    conviction = None
    if owned_before and owned_before > 0:
        conviction = round(total_shares / owned_before, 4)

    role_weight, roles = compute_role(parsed, weights)
    txn_date = max((t.txn_date for t in p_txns if t.txn_date), default=None)

    return {
        "accession_number": accession,
        "issuer_cik": parsed.issuer_cik,
        "ticker": parsed.ticker,
        "issuer_name": parsed.issuer_name,
        "insider_cik": parsed.insider_cik,
        "insider_name": parsed.insider_name,
        "is_officer": parsed.is_officer,
        "is_director": parsed.is_director,
        "is_ten_pct_owner": parsed.is_ten_pct,
        "officer_title": parsed.officer_title,
        "roles": roles,
        "txn_code": "P",
        "txn_date": txn_date,
        "shares": total_shares,
        "price": round(vwap, 4) if vwap else None,
        "total_value": round(total_value, 2),
        "shares_owned_before": owned_before,
        "shares_owned_after": owned_after,
        "role_weight": role_weight,
        "conviction_ratio": conviction,
        "new_stake": new_stake,
        "first_time_buyer": first_time,
        "source_url": source_url,
    }


# ---------------------------------------------------------------------------
# The feed orchestrator
# ---------------------------------------------------------------------------
_TICKER_OK = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


def _valid_ticker(t: str) -> bool:
    """Reject empty / placeholder / non-symbol tickers (e.g. 'N/A', 'NONE')."""
    if not t:
        return False
    t = t.strip().upper()
    if t in {"N/A", "NA", "NONE", "N.A.", "-", "."}:
        return False
    return bool(_TICKER_OK.match(t))


class SecForm4Feed:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.sec = cfg["sec"]
        self.weights = self.sec["role_weights"]
        self.min_value = self.sec.get("min_total_value", 25000)
        self.window_days = self.sec["cluster"]["window_days"]
        self.min_insiders = self.sec["cluster"]["min_distinct_insiders"]
        self.large_value = self.sec["standalone"]["large_buy_value"]
        self.alert_first_ceo_cfo = self.sec["standalone"]["alert_first_time_ceo_cfo"]
        self.first_time_min_value = self.sec["standalone"].get("first_time_min_value", 0)
        self.require_officer_or_director = self.sec["standalone"].get(
            "require_officer_or_director", True)
        # per-buy sanity ceiling: amounts above this are almost always non-open-
        # market events (mergers, share issuances, PIPEs) miscoded as code P.
        self.max_open_market_value = self.sec.get("max_open_market_value", 50000000)
        self.cross_window = cfg["fda"]["cross_feed_window_days"]
        ch = cfg["delivery"]["channels"]
        self.paid_channel = ch["sec_paid"]
        self.free_channel = ch["sec_free"]

    # -- public entrypoint ---------------------------------------------------
    def run(self, db, dispatcher, summarizer, stats, source,
            limit: Optional[int] = None) -> None:
        entries = source.get_feed_entries()
        stats.inc("feed_entries", len(entries))
        touched_tickers: set[str] = set()
        processed = 0

        for entry in entries:
            if limit is not None and processed >= limit:
                break
            if db.filing_exists(entry.accession):
                stats.inc("skipped_seen")
                continue
            processed += 1
            xml = source.get_filing_xml(entry)
            if not xml:
                stats.inc("xml_missing")
                continue
            parsed = parse_form4_xml(xml)
            if parsed is None or not parsed.ticker:
                stats.inc("parse_failed")
                _record_filing(db, entry, parsed, qualifying=False)
                continue
            stats.inc("parsed")

            first_time = not db.insider_has_prior_pbuy(parsed.insider_cik,
                                                       exclude_accession=entry.accession)
            buy = build_buy_record(parsed, entry.accession, entry.index_url,
                                   self.weights, first_time)
            _record_filing(db, entry, parsed, qualifying=bool(buy))

            if buy is None:
                stats.inc("not_purchase")
                continue
            if not _valid_ticker(buy["ticker"]):
                stats.inc("invalid_ticker")
                continue
            if self.max_open_market_value and (buy["total_value"] or 0) > self.max_open_market_value:
                # implausible as an open-market insider purchase -> almost always a
                # merger/issuance/PIPE miscoded as code P. Drop it from signals.
                stats.inc("over_sanity_ceiling")
                continue
            if (buy["total_value"] or 0) < self.min_value:
                stats.inc("below_min_value")
                # still store so cluster history is complete? No: below-noise buys
                # are excluded from signals per spec. Record filing only.
                continue

            db.insert_buy(buy)
            stats.inc("qualifying_buys")
            touched_tickers.add(buy["ticker"])

        # Cluster + standalone evaluation for each ticker touched this run.
        for ticker in touched_tickers:
            self._evaluate_ticker(db, dispatcher, summarizer, stats, ticker)

    # -- per-ticker signal evaluation ---------------------------------------
    def _evaluate_ticker(self, db, dispatcher, summarizer, stats, ticker: str) -> None:
        since = (now_utc().date() - timedelta(days=self.window_days)).isoformat()
        window_buys = db.get_window_buys(ticker, since)
        by_cik: dict[str, dict] = {}
        for b in window_buys:
            # keep the largest buy per insider for display
            cik = b["insider_cik"]
            if cik not in by_cik or (b.get("total_value") or 0) > (by_cik[cik].get("total_value") or 0):
                by_cik[cik] = b
        distinct = sorted(by_cik.keys())
        issuer_name = next((b.get("issuer_name") for b in window_buys if b.get("issuer_name")), "")
        catalyst_note = self._catalyst_note(db, ticker)

        active = db.get_active_cluster(ticker)

        if len(distinct) >= self.min_insiders:
            combined_value = sum((b.get("total_value") or 0) for b in by_cik.values())
            total_shares = sum((b.get("shares") or 0) for b in by_cik.values())
            avg_price = (combined_value / total_shares) if total_shares else None
            first_time_count = sum(1 for b in by_cik.values() if b.get("first_time_buyer"))
            dates = [b.get("txn_date") for b in by_cik.values() if b.get("txn_date")]
            facts = {
                "ticker": ticker, "issuer_name": issuer_name,
                "member_count": len(distinct), "window_days": self.window_days,
                "combined_value": combined_value, "avg_price": avg_price,
                "first_time_count": first_time_count,
                "source_url": next((b.get("source_url") for b in by_cik.values()
                                    if b.get("source_url")), ""),
                "catalyst_note": catalyst_note,
                "buys": [{
                    "name": b.get("insider_name"), "roles": b.get("roles"),
                    "shares": b.get("shares"), "price": b.get("price"),
                    "total_value": b.get("total_value"),
                    "first_time": b.get("first_time_buyer"),
                    "new_stake": b.get("new_stake"),
                } for b in sorted(by_cik.values(),
                                  key=lambda x: -(x.get("total_value") or 0))],
            }

            if active is None:
                db.create_cluster({
                    "ticker": ticker, "issuer_name": issuer_name,
                    "member_ciks": distinct, "member_count": len(distinct),
                    "alerted_count": len(distinct), "window_days": self.window_days,
                    "first_buy_date": min(dates) if dates else None,
                    "last_buy_date": max(dates) if dates else None,
                    "combined_value": combined_value, "status": "active",
                })
                key = self._cluster_key(ticker, distinct)
                html = summarizer.render_cluster(facts)
                dispatcher.dispatch(key, "cluster", self.paid_channel,
                                    self.free_channel, html, payload={"ticker": ticker})
                stats.inc("clusters_new")
            else:
                prev_members = set(active.get("member_ciks") or [])
                if set(distinct) - prev_members and len(distinct) > active.get("alerted_count", 0):
                    facts["is_upgrade"] = True
                    facts["prev_count"] = active.get("alerted_count", 0)
                    db.update_cluster(active["id"], {
                        "member_ciks": distinct, "member_count": len(distinct),
                        "alerted_count": len(distinct), "combined_value": combined_value,
                        "last_buy_date": max(dates) if dates else None,
                    })
                    key = self._cluster_key(ticker, distinct)
                    html = summarizer.render_cluster(facts)
                    dispatcher.dispatch(key, "cluster_upgrade", self.paid_channel,
                                        self.free_channel, html, payload={"ticker": ticker})
                    stats.inc("clusters_upgraded")
            return  # ticker is in a cluster -> no standalone alerts

        # No cluster: close any stale active cluster, then check standalone buys.
        if active is not None:
            db.update_cluster(active["id"], {"status": "closed"})

        for b in by_cik.values():
            is_officer_or_director = bool(b.get("is_officer") or b.get("is_director"))
            is_large = ((b.get("total_value") or 0) >= self.large_value
                        and (is_officer_or_director or not self.require_officer_or_director))
            is_first_ceo_cfo = (self.alert_first_ceo_cfo and b.get("first_time_buyer")
                                and (b.get("role_weight") or 0) >= self.weights["cfo"]
                                and (b.get("total_value") or 0) >= self.first_time_min_value)
            if not (is_large or is_first_ceo_cfo):
                continue
            facts = {
                "ticker": ticker, "issuer_name": b.get("issuer_name"),
                "insider_name": b.get("insider_name"), "roles": b.get("roles"),
                "shares": b.get("shares"), "price": b.get("price"),
                "total_value": b.get("total_value"),
                "first_time": b.get("first_time_buyer"),
                "new_stake": b.get("new_stake"),
                "conviction_ratio": b.get("conviction_ratio"),
                "catalyst_note": catalyst_note,
                "source_url": b.get("source_url"),
            }
            key = f"sec:single:{b['accession_number']}"
            html = summarizer.render_single_buy(facts)
            if dispatcher.dispatch(key, "single_buy", self.paid_channel,
                                   self.free_channel, html, payload={"ticker": ticker}):
                stats.inc("single_buys")

    def _catalyst_note(self, db, ticker: str) -> Optional[str]:
        cats = db.get_catalysts_for_ticker_within(ticker, self.cross_window)
        if not cats:
            return None
        cats.sort(key=lambda c: str(c.get("expected_date") or "9999"))
        c = cats[0]
        when = c.get("expected_date") or "TBD"
        return f"⚡ This company has a {c.get('catalyst_type')} catalyst expected {when}."

    @staticmethod
    def _cluster_key(ticker: str, members: list[str]) -> str:
        h = hashlib.sha1((",".join(sorted(members))).encode()).hexdigest()[:10]
        return f"sec:cluster:{ticker}:{len(members)}:{h}"


def _record_filing(db, entry: FeedEntry, parsed: Optional[ParsedForm4],
                   qualifying: bool) -> None:
    db.insert_filing({
        "accession_number": entry.accession,
        "form_type": "4",
        "issuer_cik": parsed.issuer_cik if parsed else "",
        "ticker": parsed.ticker if parsed else "",
        "issuer_name": parsed.issuer_name if parsed else entry.title,
        "filed_at": now_utc().isoformat(),
        "source_url": entry.index_url,
        "has_qualifying_buy": qualifying,
    })
