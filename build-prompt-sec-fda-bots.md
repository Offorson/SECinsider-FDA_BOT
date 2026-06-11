# Build Prompt — SEC Insider Cluster Bot + FDA Catalyst Bot

> This file is the **source of truth** for the project. Re-read it before major decisions.

You are building a production-ready alert system. Build exactly what is described — do not
simplify the architecture, do not skip modules, and do not substitute paid services for the
free ones specified.

## PROJECT OVERVIEW

Build a single Python codebase that runs two automated financial alert feeds:

1. **SEC Insider Cluster Bot** — monitors SEC EDGAR for Form 4 insider open-market purchases
   and detects "cluster buys" (multiple insiders at the same company buying within a short
   window).
2. **FDA Catalyst Bot** — maintains a calendar of upcoming biotech catalysts (PDUFA dates,
   advisory committee meetings, trial readouts) and fires alerts before events and instantly
   when decisions/results are announced.

Both feeds share one infrastructure: one repo, one database, one delivery layer, one config
system. They publish to separate Telegram channels.

### End goals (the system is "done" when all of these are true)

- Runs unattended on GitHub Actions scheduled workflows with zero servers and zero hosting cost.
- Never sends a duplicate alert, even if a workflow run crashes mid-way and restarts.
- A new Form 4 cluster alert reaches Telegram within ~10 minutes of the filing appearing on EDGAR.
- The FDA calendar can be manually seeded via a simple CSV import command, then maintains itself
  by monitoring press release feeds.
- Every alert carries the disclaimer footer. The system never produces language that recommends
  buying or selling.
- I can deploy it by following the README alone, with no prior DevOps knowledge.

## TECH STACK (fixed — do not deviate)

- **Language:** Python 3.11+
- **Scheduler/host:** GitHub Actions scheduled workflows (cron). Assume a public repo so Actions
  minutes are free.
- **Database:** Supabase (Postgres) free tier, accessed via supabase-py. All state lives here —
  never in local files, since Actions runners are ephemeral.
- **Delivery:** Telegram Bot API via plain HTTPS calls with requests (no heavy bot framework —
  the bot only posts to channels, it does not handle incoming messages).
- **AI summaries:** Google Gemini API free tier. CRITICAL: implement this as a swappable module
  (`summarizer.py`) with three backends selected by config: `template` (pure string formatting,
  no API, the default), `gemini`, and `claude` (stub for later). If the Gemini call fails or hits
  rate limits, ALWAYS fall back to the template — an alert must never be dropped because the AI
  call failed.
- **Parsing:** lxml or xmltodict for Form 4 XML, feedparser for Atom/RSS feeds.
- No paid services anywhere. No Docker required. No web frontend — this is a headless pipeline.

## REPO STRUCTURE

```
/src
  /feeds
    sec_form4.py        # EDGAR polling + Form 4 parsing
    fda_catalysts.py    # catalyst calendar + press release monitoring
  /core
    db.py               # Supabase client + all queries
    telegram.py         # channel posting, retry logic
    summarizer.py       # template | gemini | claude backends
    config.py           # loads config.yaml + env secrets
  /jobs
    poll_sec.py         # entrypoint: run by Actions every 10 min
    poll_fda.py         # entrypoint: run by Actions every 15 min
    daily_digest.py     # entrypoint: run daily
    release_delayed.py  # entrypoint: posts 24h-delayed alerts to free channels
  /scripts
    seed_catalysts.py   # CSV import for manual PDUFA seeding
    backfill_form4.py   # pulls last N days of Form 4s to warm up the cluster window
schema.sql              # full Supabase schema, runnable as-is
config.yaml             # all tunable parameters (thresholds, windows, channel IDs)
.env.example            # every secret named and explained
.github/workflows/*.yml # the scheduled workflows
README.md               # full deploy guide written for a beginner
requirements.txt
```

## MODULE 1 — SEC INSIDER CLUSTER BOT

Data source: SEC EDGAR. Use the official JSON/XML endpoints and the latest-filings Atom feed
filtered to Form 4. HARD RULES: send a User-Agent header in the format `AppName contact@email.com`
(read from env), stay at or under 9 requests/second with a rate limiter, never scrape EDGAR HTML
pages, and back off on any 429/403.

### Pipeline per run

1. Fetch the latest Form 4 filings feed.
2. For each filing not already in the filings table, fetch the filing's XML and parse:
   ticker/issuer, insider name, insider role(s) (officer title, director, 10% owner flags),
   transaction code, transaction date, shares, price per share, total value, shares owned
   before/after.
3. Store every parsed filing. Dedupe on EDGAR accession number (unique constraint in the DB —
   rely on the constraint, not just application logic).
4. Filter: only transaction code P (open-market purchase). Explicitly exclude codes A, M, F, G, S
   and derivative-only filings. Apply a configurable minimum total value (default $25,000) to cut
   noise.

### Signal scoring (compute and store for every qualifying buy)

- Role weight: CEO/CFO = 3, President/COO = 2.5, other C-suite = 2, Director = 1, 10% owner = 1.5.
  Multiple roles take the max.
- Conviction ratio: shares bought ÷ shares owned before (handle zero-before as a special
  "new stake" flag).
- First-time buyer flag: true if this insider (matched by EDGAR CIK, not by name string) has no
  prior code-P purchase in our database. Note in the README that this flag becomes more reliable
  as history accumulates, and that `backfill_form4.py` should be run once at setup to pull 90 days
  of history.

### Cluster detection (the core product)

- Rolling window of N days (default 14, config-tunable) per ticker.
- A cluster fires when ≥3 DISTINCT insiders (by CIK) at the same issuer have qualifying buys
  inside the window.
- When a cluster fires, send ONE cluster alert summarizing all participating buys (names, roles,
  amounts, average price, combined value, any first-time-buyer flags). Record the cluster in a
  clusters table so the same cluster doesn't re-fire on every new run; if a NEW insider joins an
  already-alerted cluster, send an "upgrade" alert (e.g. "now 4 insiders").
- Single large buys (no cluster) above a higher threshold (default $500,000 or any first-time
  CEO/CFO buy) also generate a standalone alert, marked clearly as non-cluster.

## MODULE 2 — FDA CATALYST BOT

Catalyst calendar: a catalysts table holding: ticker, company, drug name, catalyst type (PDUFA,
AdCom, Phase 1/2/3 readout, CRL, approval), expected date (supports month-only precision — store a
precision field), source URL, status (upcoming / hit / passed-unresolved).

Manual seeding: `seed_catalysts.py` imports a CSV with columns matching the table. Validate rows,
report rejects, never crash on a bad row. This is how I'll load the initial 100–200 known PDUFA
dates.

### Automated maintenance

1. Press release monitoring: poll public RSS feeds (GlobeNewswire and PR Newswire public feeds,
   filterable by keyword) every 15 minutes for phrases like "PDUFA", "FDA accepts",
   "target action date", "complete response letter", "FDA approves", "topline results". When
   matched: send an instant news alert AND, where a date is extractable, insert/update the
   catalyst calendar. Use the summarizer module to extract the structured fields (ticker, drug,
   date) from the press release text when the backend is gemini; with the template backend, alert
   with the headline + link and flag the entry for my manual review (a `needs_review` boolean).
2. ClinicalTrials.gov API v2: for tickers present in the catalyst table, watch sponsor trials for
   status changes (e.g. to "Completed") and primary-completion-date changes; alert on changes.

Alert schedule (daily job): reminders at T-30, T-14, T-7, and T-1 days before each upcoming
catalyst (configurable). On the day a PDUFA passes with no news, mark passed-unresolved and alert
that a decision is imminent/overdue.

### Cross-feed signal (unique feature)

Whenever an SEC cluster or large insider buy fires on a ticker that has a catalyst within the next
90 days, append a highlighted note to the alert: "⚡ This company has [catalyst type] expected
[date]." This requires the two feeds to share the database — that's why this is one codebase.

## DELIVERY LAYER

Four Telegram channels (IDs from config): SEC-free, SEC-paid, FDA-free, FDA-paid.

- Real-time alerts go to PAID channels immediately.
- Every alert is also queued in a `delayed_queue` table; `release_delayed.py` (runs hourly) posts
  items older than 24h to the matching FREE channel.
- Telegram posting must retry with exponential backoff on failure and respect Telegram rate
  limits; a failed post stays queued, never lost.
- Message format: clean, emoji-light, mobile-readable, with the EDGAR/press-release source link.
  EVERY message ends with: `📋 Public filing data. Not investment advice.`
- Use Telegram HTML parse mode; escape all dynamic text.

## NON-NEGOTIABLE ENGINEERING REQUIREMENTS

- Idempotency everywhere: any job can crash and re-run safely with no duplicate alerts. Enforce
  with DB unique constraints + an `alerts_sent` table checked before every send.
- All secrets via environment variables (GitHub Actions secrets): Supabase URL/key, Telegram bot
  token, Gemini key, SEC contact email. Zero secrets in code or config.yaml. `.env.example`
  documents every one.
- All tunables in config.yaml: thresholds, windows, role weights, reminder days, channel IDs,
  summarizer backend, feed URLs.
- Dry-run mode (`--dry-run` flag on every job): full pipeline runs but prints alerts to stdout
  instead of posting. The README's first deploy step uses dry-run.
- Logging: structured, human-readable logs of every run (counts fetched/parsed/alerted/skipped) so
  GitHub Actions logs are useful for debugging.
- Graceful degradation: if EDGAR is down, log and exit 0 (Actions shouldn't show failure spam for
  upstream outages); if Supabase is down, exit 1 (real failure). If Gemini fails, fall back to
  template silently.
- Free-tier discipline: keep each Actions run under ~2 minutes typical; batch DB writes; never poll
  faster than the schedule specifies.

## WHAT NOT TO DO

- Do not scrape HTML where an API/feed exists. Do not exceed SEC's rate limits. Do not omit the
  User-Agent.
- Do not use any paid API, paid hosting, or services requiring a credit card.
- Do not generate any text that recommends, suggests, or implies buying/selling a security. Alerts
  report facts only. If using Gemini for summaries, the prompt you write for it must explicitly
  instruct factual reporting with no opinion or advice language.
- Do not store state on the runner filesystem. Do not use SQLite. Everything stateful goes to
  Supabase.
- Do not build a web UI, a user-facing bot command handler, or subscription management — Whop
  handles channel access externally.
- Do not invent placeholder data sources. If a feed URL needs to be confirmed, put it in
  config.yaml with a clear comment and list it in the README's "confirm before launch" checklist.

## DELIVERABLES

1. Complete working code per the repo structure above.
2. `schema.sql` I can paste into the Supabase SQL editor in one shot (tables, unique constraints,
   indexes).
3. GitHub Actions workflow files for all four jobs with correct cron schedules.
4. A README that walks a beginner through: creating the Supabase project and running schema.sql →
   creating the Telegram bot via BotFather and getting channel IDs → getting a Gemini API key from
   Google AI Studio → adding GitHub secrets → running backfill + seed scripts → a dry-run test →
   going live. Include a "confirm before launch" checklist and a troubleshooting section.
5. A sample `catalysts_seed.csv` with 5 example rows showing the expected format.

Build the full system. Where you face a genuine design ambiguity, choose the simpler, more reliable
option and note the decision in the README rather than asking.
