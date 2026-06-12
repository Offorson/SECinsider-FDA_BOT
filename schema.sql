-- =============================================================================
--  schema.sql  —  SEC Insider Cluster Bot + FDA Catalyst Bot
--  Paste this whole file into the Supabase SQL Editor and Run. Safe to re-run
--  (idempotent: IF NOT EXISTS everywhere). All bot state lives here.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- filings: one row per Form 4 we have already fetched & parsed. The accession
-- number is the EDGAR-unique id; the UNIQUE constraint is the dedupe guard that
-- lets a crashed run re-run without re-processing.
-- ---------------------------------------------------------------------------
create table if not exists filings (
    id                bigint generated always as identity primary key,
    accession_number  text not null unique,
    form_type         text,
    issuer_cik        text,
    ticker            text,
    issuer_name       text,
    filed_at          timestamptz,
    period_of_report  date,
    source_url        text,
    has_qualifying_buy boolean not null default false,
    created_at        timestamptz not null default now()
);
create index if not exists idx_filings_filed_at on filings (filed_at desc);
create index if not exists idx_filings_ticker   on filings (ticker);

-- ---------------------------------------------------------------------------
-- insider_buys: one row per qualifying (code P) buy, aggregated to one record
-- per filing (a Form 4 = one reporting owner). Drives scoring + clustering.
-- UNIQUE(accession_number) keeps re-runs idempotent.
-- ---------------------------------------------------------------------------
create table if not exists insider_buys (
    id                  bigint generated always as identity primary key,
    accession_number    text not null unique,
    issuer_cik          text,
    ticker              text not null,
    issuer_name         text,
    insider_cik         text not null,
    insider_name        text,
    is_officer          boolean not null default false,
    is_director         boolean not null default false,
    is_ten_pct_owner    boolean not null default false,
    officer_title       text,
    roles               text,            -- human-readable role summary
    txn_code            text not null,
    txn_date            date not null,
    shares              numeric,
    price               numeric,
    total_value         numeric,
    shares_owned_before numeric,
    shares_owned_after  numeric,
    role_weight         numeric,
    conviction_ratio    numeric,
    new_stake           boolean not null default false,
    first_time_buyer    boolean not null default false,
    source_url          text,
    created_at          timestamptz not null default now()
);
create index if not exists idx_buys_ticker_date on insider_buys (ticker, txn_date desc);
create index if not exists idx_buys_insider_code on insider_buys (insider_cik, txn_code);

-- ---------------------------------------------------------------------------
-- clusters: one ACTIVE cluster per ticker. member_ciks holds the distinct
-- insiders currently in-window; alerted_count is the size at the last alert so
-- we can fire an "upgrade" when a new insider joins. Partial unique index keeps
-- a single active cluster per ticker.
-- ---------------------------------------------------------------------------
create table if not exists clusters (
    id              bigint generated always as identity primary key,
    ticker          text not null,
    issuer_name     text,
    member_ciks     jsonb not null default '[]'::jsonb,
    member_count    integer not null default 0,
    alerted_count   integer not null default 0,
    window_days     integer,
    first_buy_date  date,
    last_buy_date   date,
    combined_value  numeric,
    status          text not null default 'active',   -- active | closed
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create unique index if not exists uniq_active_cluster_per_ticker
    on clusters (ticker) where status = 'active';

-- ---------------------------------------------------------------------------
-- catalysts: the FDA event calendar. date_precision records whether expected_date
-- is exact (day), month-only, or quarter-only. needs_review flags auto-captured
-- rows for manual confirmation.
-- ---------------------------------------------------------------------------
create table if not exists catalysts (
    id              bigint generated always as identity primary key,
    ticker          text not null,
    company         text,
    drug            text,
    catalyst_type   text not null,           -- PDUFA | AdCom | Phase 1/2/3 | CRL | Approval | Readout
    expected_date   date,
    date_precision  text not null default 'day',  -- day | month | quarter
    source_url      text,
    status          text not null default 'upcoming', -- upcoming | hit | passed-unresolved
    needs_review    boolean not null default false,
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
-- Natural-key dedupe for seeding & auto-capture (drug may be null -> coalesce).
create unique index if not exists uniq_catalyst_natural
    on catalysts (ticker, catalyst_type, coalesce(drug, ''), coalesce(expected_date, '1900-01-01'::date));
create index if not exists idx_catalyst_status_date on catalysts (status, expected_date);
create index if not exists idx_catalyst_ticker on catalysts (ticker);

-- ---------------------------------------------------------------------------
-- ct_trials: ClinicalTrials.gov v2 watch state, to detect status / completion-
-- date changes for sponsors of tickers we track.
-- ---------------------------------------------------------------------------
create table if not exists ct_trials (
    id                      bigint generated always as identity primary key,
    nct_id                  text not null unique,
    ticker                  text,
    sponsor                 text,
    overall_status          text,
    primary_completion_date text,
    last_checked            timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);
create index if not exists idx_ct_ticker on ct_trials (ticker);

-- ---------------------------------------------------------------------------
-- press_seen: GUIDs of press-release items already processed, so the same item
-- never alerts twice.
-- ---------------------------------------------------------------------------
create table if not exists press_seen (
    id        bigint generated always as identity primary key,
    guid      text not null unique,
    feed      text,
    title     text,
    link      text,
    seen_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- alerts_sent: THE idempotency ledger. Every outbound alert claims a unique
-- dedup_key here BEFORE sending. status: pending (claimed, not yet confirmed),
-- sent (delivered). A pending row older than the stale window is reclaimable so
-- a crash mid-send is retried instead of lost. The UNIQUE(dedup_key) constraint
-- is what makes "never duplicate" hold even across concurrent/restarted runs.
-- ---------------------------------------------------------------------------
create table if not exists alerts_sent (
    id          bigint generated always as identity primary key,
    dedup_key   text not null unique,
    feed        text,                 -- sec | fda
    channel     text,                 -- channel id the alert went to
    alert_type  text,                 -- cluster | cluster_upgrade | single_buy | news | reminder | ...
    status      text not null default 'pending',  -- pending | sent
    payload     jsonb,
    created_at  timestamptz not null default now(),
    sent_at     timestamptz
);
create index if not exists idx_alerts_status on alerts_sent (status, created_at);

-- ---------------------------------------------------------------------------
-- delayed_queue: every real-time (paid) alert is also queued here; the hourly
-- release job posts items to the matching FREE channel once available_at passes.
-- UNIQUE(dedup_key) prevents double-enqueue.
-- ---------------------------------------------------------------------------
create table if not exists delayed_queue (
    id            bigint generated always as identity primary key,
    dedup_key     text not null unique,
    feed          text,
    free_channel  text,
    html          text not null,
    available_at  timestamptz not null,
    released      boolean not null default false,
    released_at   timestamptz,
    created_at    timestamptz not null default now()
);
create index if not exists idx_delayed_due on delayed_queue (released, available_at);

-- ---------------------------------------------------------------------------
-- alert_performance: track record. One row per fired SEC alert (cluster /
-- upgrade / single buy). The weekly recap job fills alert_price (close on the
-- alert date) + 1W/1M/3M returns from a free price source. UNIQUE(dedup_key)
-- ties it to the alert and keeps recording idempotent.
-- ---------------------------------------------------------------------------
create table if not exists alert_performance (
    id              bigint generated always as identity primary key,
    dedup_key       text not null unique,
    feed            text,
    alert_type      text,
    ticker          text not null,
    alert_date      date not null,
    alert_price     numeric,
    last_price      numeric,
    last_priced_at  date,
    ret_1w          numeric,
    ret_1m          numeric,
    ret_3m          numeric,
    ret_peak        numeric,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists idx_alertperf_ticker on alert_performance (ticker);
create index if not exists idx_alertperf_date on alert_performance (alert_date);

-- =============================================================================
--  End of schema.
-- =============================================================================
