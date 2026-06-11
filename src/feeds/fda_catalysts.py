"""FDA Catalyst Bot — Module 2.

Maintains the catalyst calendar and reacts to news:

* Press-release monitoring (GlobeNewswire / PR Newswire RSS): keyword match ->
  instant news alert; structured extraction (gemini) updates the calendar, else
  the row is flagged ``needs_review`` for manual confirmation.
* ClinicalTrials.gov API v2: for tracked tickers, watch sponsor trials for
  status / primary-completion-date changes and alert on change.
* Reminders (daily): T-30 / T-14 / T-7 / T-1 before each upcoming catalyst.
* Overdue: a passed PDUFA with no news -> mark ``passed-unresolved`` + alert.

Sources are abstracted (live vs fixture) so the pipeline runs offline in tests.
Like SEC, upstream outages are swallowed (log + continue) so a feed being down
never fails the job — only a Supabase outage does.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import feedparser
import requests

from src.core.db import now_utc

log = logging.getLogger("fda")

# keyword (lowercase) -> catalyst_type
KEYWORD_TYPE = [
    ("complete response letter", "CRL"),
    ("crl", "CRL"),
    ("advisory committee", "AdCom"),
    ("adcom", "AdCom"),
    ("pdufa", "PDUFA"),
    ("target action date", "PDUFA"),
    ("priority review", "PDUFA"),
    ("fda accepts", "PDUFA"),
    ("fda acceptance", "PDUFA"),
    ("accepted for review", "PDUFA"),
    ("fda approves", "Approval"),
    ("fda approval", "Approval"),
    ("topline results", "Readout"),
    ("topline data", "Readout"),
    ("readout", "Readout"),
]

_TICKER_RE = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE American|NYSE|NYSE MKT|OTCQB|OTCQX|OTC|CBOE)[:\s]+"
    r"([A-Z][A-Z\.]{0,5})\)")


def extract_ticker(text: str) -> str:
    m = _TICKER_RE.search(text or "")
    return m.group(1).replace(".", "") if m else ""


def classify(text: str) -> tuple[list[str], Optional[str]]:
    """Return (matched_keywords, best_catalyst_type) for a piece of text."""
    low = (text or "").lower()
    matched, ctype = [], None
    for kw, t in KEYWORD_TYPE:
        if kw in low:
            matched.append(kw)
            if ctype is None:
                ctype = t
    return matched, ctype


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@dataclass
class PressItem:
    guid: str
    title: str
    summary: str
    link: str
    feed: str


class LivePressSource:
    def __init__(self, cfg, user_agent: str) -> None:
        self.feeds = cfg["fda"]["press_feeds"]
        self.timeout = cfg["sec"].get("request_timeout_sec", 20)
        self.headers = {"User-Agent": user_agent}
        self.session = requests.Session()

    def get_items(self) -> list[PressItem]:
        items: list[PressItem] = []
        for feed in self.feeds:
            url = feed.get("url", "")
            if not url or "..." in url or "CONFIRM" in url:
                log.warning("Skipping unconfirmed feed URL: %s", feed.get("name"))
                continue
            try:
                resp = self.session.get(url, headers=self.headers, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning("Press feed %s unavailable: %s", feed.get("name"), exc)
                continue
            parsed = feedparser.parse(resp.content)
            for e in parsed.entries:
                guid = e.get("id") or e.get("link") or e.get("title", "")
                items.append(PressItem(
                    guid=guid, title=e.get("title", ""),
                    summary=re.sub("<[^>]+>", " ", e.get("summary", "")),
                    link=e.get("link", ""), feed=feed.get("name", "")))
        return items


class LiveCtSource:
    def __init__(self, cfg, user_agent: str) -> None:
        self.api = cfg["fda"]["clinicaltrials_api"]
        self.timeout = cfg["sec"].get("request_timeout_sec", 20)
        self.headers = {"User-Agent": user_agent}
        self.session = requests.Session()

    def get_studies(self, company: str) -> list[dict]:
        params = {"query.spons": company, "pageSize": 50, "format": "json"}
        try:
            resp = self.session.get(self.api, params=params,
                                    headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("ClinicalTrials.gov query failed for %s: %s", company, exc)
            return []
        out = []
        for study in data.get("studies", []):
            ps = study.get("protocolSection", {})
            ident = ps.get("identificationModule", {})
            status = ps.get("statusModule", {})
            spons = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
            out.append({
                "nct_id": ident.get("nctId", ""),
                "overall_status": status.get("overallStatus", ""),
                "primary_completion_date":
                    status.get("primaryCompletionDateStruct", {}).get("date", ""),
                "sponsor": spons.get("name", ""),
            })
        return [s for s in out if s["nct_id"]]


# ---------------------------------------------------------------------------
# Date helpers (month/quarter precision support)
# ---------------------------------------------------------------------------
def effective_end_date(expected_date: str, precision: str) -> Optional[date]:
    """The last day an event could still occur, given its precision."""
    if not expected_date:
        return None
    try:
        d = datetime.strptime(expected_date[:10], "%Y-%m-%d").date()
    except ValueError:
        try:
            d = datetime.strptime(expected_date[:7], "%Y-%m").date()
            precision = "month"
        except ValueError:
            return None
    if precision == "month":
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)
    if precision == "quarter":
        q_end_month = ((d.month - 1) // 3 + 1) * 3
        last = calendar.monthrange(d.year, q_end_month)[1]
        return date(d.year, q_end_month, last)
    return d


# ---------------------------------------------------------------------------
# The feed orchestrator
# ---------------------------------------------------------------------------
class FdaCatalystFeed:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.keywords = [k.lower() for k in cfg["fda"]["keywords"]]
        self.reminder_days = cfg["fda"]["reminder_days"]
        ch = cfg["delivery"]["channels"]
        self.paid_channel = ch["fda_paid"]
        self.free_channel = ch["fda_free"]
        self.summarizer_backend = cfg["summarizer"]["backend"]

    # -- press monitoring ----------------------------------------------------
    def run_press(self, db, dispatcher, summarizer, stats, source,
                  limit: Optional[int] = None) -> None:
        items = source.get_items()
        stats.inc("press_items", len(items))
        processed = 0
        for item in items:
            if limit is not None and processed >= limit:
                break
            if db.press_seen(item.guid):
                stats.inc("press_skipped_seen")
                continue
            text = "%s\n%s" % (item.title, item.summary)
            matched, ctype = classify(text)
            if not any(k in text.lower() for k in self.keywords):
                db.mark_press_seen({"guid": item.guid, "feed": item.feed,
                                    "title": item.title, "link": item.link})
                stats.inc("press_no_match")
                continue
            processed += 1
            ticker = extract_ticker(text)

            fields = summarizer.extract_catalyst_fields(item.title, item.summary)
            needs_review = fields is None
            exp_date = None
            precision = "day"
            if fields:
                ticker = (fields.get("ticker") or ticker or "").upper()
                ctype = fields.get("catalyst_type") or ctype
                exp_date = fields.get("date")
                precision = fields.get("date_precision") or "day"

            facts = {
                "ticker": ticker, "title": item.title, "link": item.link,
                "matched": matched or [k for k in self.keywords if k in text.lower()],
                "needs_review": needs_review,
            }
            base = "fda:news:" + hashlib.sha1(item.guid.encode()).hexdigest()[:12]
            html = summarizer.render_press(facts)
            if dispatcher.dispatch(base, "news", self.paid_channel,
                                   self.free_channel, html, payload={"ticker": ticker}):
                stats.inc("news_alerts")

            if ticker and ctype:
                _, created = db.upsert_catalyst({
                    "ticker": ticker, "company": "", "drug": "",
                    "catalyst_type": ctype, "expected_date": exp_date,
                    "date_precision": precision,
                    "status": "upcoming", "needs_review": needs_review,
                    "source_url": item.link, "notes": item.title[:280],
                })
                if created:
                    stats.inc("catalysts_captured")

            db.mark_press_seen({"guid": item.guid, "feed": item.feed,
                                "title": item.title, "link": item.link})

    # -- clinicaltrials.gov watch -------------------------------------------
    def run_clinicaltrials(self, db, dispatcher, summarizer, stats, source) -> None:
        company_ticker: dict[str, str] = {}
        for c in db.get_upcoming_catalysts():
            if c.get("company") and c.get("ticker"):
                company_ticker.setdefault(c["company"], c["ticker"])
        for company, ticker in company_ticker.items():
            for s in source.get_studies(company):
                prev = db.get_ct_trial(s["nct_id"])
                record = {"nct_id": s["nct_id"], "ticker": ticker,
                          "sponsor": s.get("sponsor"),
                          "overall_status": s.get("overall_status"),
                          "primary_completion_date": s.get("primary_completion_date"),
                          "last_checked": now_utc().isoformat()}
                if prev is None:
                    db.upsert_ct_trial(record)
                    stats.inc("ct_baseline")
                    continue
                changed = []
                if prev.get("overall_status") != s.get("overall_status"):
                    changed.append("status %s -> %s" % (prev.get("overall_status"),
                                                        s.get("overall_status")))
                if prev.get("primary_completion_date") != s.get("primary_completion_date"):
                    changed.append("primary completion %s -> %s" % (
                        prev.get("primary_completion_date"),
                        s.get("primary_completion_date")))
                if changed:
                    facts = {"ticker": ticker,
                             "title": "Trial %s update: %s" % (s["nct_id"], "; ".join(changed)),
                             "link": "https://clinicaltrials.gov/study/%s" % s["nct_id"],
                             "matched": ["clinicaltrials.gov"], "needs_review": False}
                    base = "fda:ct:%s:%s" % (s["nct_id"], hashlib.sha1(
                        "|".join(changed).encode()).hexdigest()[:10])
                    html = summarizer.render_press(facts)
                    if dispatcher.dispatch(base, "ct_change", self.paid_channel,
                                           self.free_channel, html, payload={"ticker": ticker}):
                        stats.inc("ct_changes")
                db.upsert_ct_trial(record)

    # -- reminders (daily) ---------------------------------------------------
    def run_reminders(self, db, dispatcher, summarizer, stats) -> None:
        today = now_utc().date()
        for c in db.get_upcoming_catalysts():
            if (c.get("date_precision") or "day") != "day" or not c.get("expected_date"):
                continue
            try:
                ed = datetime.strptime(str(c["expected_date"])[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            days_until = (ed - today).days
            if days_until in self.reminder_days:
                html = _reminder_html(c, days_until)
                base = "fda:reminder:%s:T%s" % (c["id"], days_until)
                if dispatcher.dispatch(base, "reminder", self.paid_channel,
                                       self.free_channel, html, payload={"ticker": c.get("ticker")}):
                    stats.inc("reminders")

    # -- overdue / passed-unresolved (daily) --------------------------------
    def run_overdue(self, db, dispatcher, summarizer, stats) -> None:
        today = now_utc().date()
        for c in db.get_upcoming_catalysts():
            eff = effective_end_date(str(c.get("expected_date") or ""),
                                     c.get("date_precision") or "day")
            if eff is None or eff >= today:
                continue
            db.update_catalyst_status(c["id"], "passed-unresolved")
            html = _overdue_html(c)
            base = "fda:overdue:%s" % c["id"]
            if dispatcher.dispatch(base, "overdue", self.paid_channel,
                                   self.free_channel, html, payload={"ticker": c.get("ticker")}):
                stats.inc("overdue")


# ---------------------------------------------------------------------------
# Reminder/overdue HTML (factual, template — these never use AI)
# ---------------------------------------------------------------------------
def _fmt_date(d) -> str:
    from datetime import datetime
    if not d:
        return "TBD"
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return str(d)


def _catalyst_line(c: dict) -> str:
    from src.core.telegram import escape_html
    bits = ["$" + escape_html(c.get("ticker") or "?")]
    if c.get("drug"):
        bits.append(escape_html(c["drug"]))
    bits.append(escape_html(c.get("catalyst_type") or "catalyst"))
    return " · ".join(bits)


def _reminder_html(c: dict, days_until: int) -> str:
    from src.core.telegram import escape_html
    when = "tomorrow" if days_until == 1 else ("in %d days" % days_until)
    lines = ["⏰ <b>Catalyst Reminder · %s</b>" % escape_html(when),
             "",
             _catalyst_line(c),
             "Expected: <b>%s</b>" % escape_html(_fmt_date(c.get("expected_date")))]
    if c.get("source_url"):
        url = escape_html(c["source_url"])
        lines.append("")
        lines.append('🔗 <a href="%s">Source</a>' % url)
    return "\n".join(lines)


def _overdue_html(c: dict) -> str:
    from src.core.telegram import escape_html
    lines = ["⌛ <b>Decision Overdue</b>",
             "",
             _catalyst_line(c),
             "Expected by <b>%s</b> — no announcement detected yet."
             % escape_html(_fmt_date(c.get("expected_date")))]
    if c.get("source_url"):
        url = escape_html(c["source_url"])
        lines.append("")
        lines.append('🔗 <a href="%s">Source</a>' % url)
    return "\n".join(lines)
