-- Relational layer (Postgres), Phase 1-2 design. Raw ticks/fills stay in Parquet (immutable lake);
-- this holds entities, point-in-time snapshots and derived state. Every snapshot table is append-only
-- with an as-of timestamp, so any metric is reproducible as of any past time.

CREATE TABLE markets (
  market_id        TEXT PRIMARY KEY,
  condition_id     TEXT NOT NULL UNIQUE,
  question_id      TEXT,
  event_id         TEXT NOT NULL,
  slug             TEXT,
  question         TEXT,
  category         TEXT,          -- our playbook category (gamma.CATEGORY_TAGS)
  tags             TEXT[],
  version          TEXT,          -- v1 = CTF tokenId, v2 = PositionManager positionId
  neg_risk         BOOLEAN,
  end_date         TIMESTAMPTZ,
  resolution_source TEXT,
  description      TEXT,
  first_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE outcomes (
  asset_id      TEXT PRIMARY KEY,   -- tokenId (v1) or positionId (v2)
  condition_id  TEXT NOT NULL REFERENCES markets(condition_id),
  outcome_index SMALLINT NOT NULL,
  outcome       TEXT
);

CREATE TABLE market_snapshots (      -- from gamma_markets (slim) every refresh
  snapshot_ts  TIMESTAMPTZ NOT NULL,
  market_id    TEXT NOT NULL,
  best_bid NUMERIC, best_ask NUMERIC, last_trade_price NUMERIC,
  volume_24h NUMERIC, liquidity NUMERIC, fee_rate NUMERIC,
  uma_resolution_status TEXT, accepting_orders BOOLEAN, closed BOOLEAN,
  PRIMARY KEY (market_id, snapshot_ts)
);

CREATE TABLE wallets (
  wallet          TEXT PRIMARY KEY,
  first_seen_block BIGINT,
  first_funding_from TEXT,           -- insider-signature features (Phase 2)
  first_funding_ts TIMESTAMPTZ,
  cluster_id      TEXT,              -- linked-wallet entity (shared funding / lockstep)
  is_protocol     BOOLEAN DEFAULT FALSE,
  label           TEXT               -- e.g. 'market_maker', 'minter'
);

CREATE TABLE position_snapshots (    -- from positions.position_book(as_of) + mark
  as_of        TIMESTAMPTZ NOT NULL,
  wallet       TEXT NOT NULL,
  asset_id     TEXT NOT NULL,
  position     NUMERIC, cost_basis NUMERIC, avg_entry NUMERIC,
  realized_pnl NUMERIC, unrealized_pnl NUMERIC, fees_paid NUMERIC,
  maker_share  NUMERIC, first_fill TIMESTAMPTZ, last_fill TIMESTAMPTZ,
  source       TEXT NOT NULL,        -- 'chain' | 'data_api'
  PRIMARY KEY (as_of, wallet, asset_id, source)
);

CREATE TABLE holder_snapshots (      -- Data API v2 /holders?include_pnl=true
  snapshot_ts  TIMESTAMPTZ NOT NULL,
  condition_id TEXT NOT NULL,
  asset_id     TEXT NOT NULL,
  wallet       TEXT NOT NULL,
  amount NUMERIC, avg_price NUMERIC, entry_cost NUMERIC,
  unrealized_pnl NUMERIC, realized_pnl NUMERIC,
  entry_known  BOOLEAN GENERATED ALWAYS AS (avg_price > 0) STORED,
  PRIMARY KEY (snapshot_ts, asset_id, wallet)
);

CREATE TABLE holder_moves (
  detected_ts TIMESTAMPTZ NOT NULL, condition_id TEXT, asset_id TEXT, wallet TEXT,
  kind TEXT CHECK (kind IN ('NEW','ADD','REDUCE','EXIT')), flip BOOLEAN,
  amount_prev NUMERIC, amount_cur NUMERIC, entry_cur NUMERIC
);

CREATE TABLE wallet_scores (         -- Phase 2: one row per definition x window, point-in-time
  as_of TIMESTAMPTZ NOT NULL, wallet TEXT NOT NULL, definition TEXT NOT NULL,
  category TEXT, horizon TEXT, score NUMERIC, confidence NUMERIC, n_independent_events INT,
  PRIMARY KEY (as_of, wallet, definition, category, horizon)
);

CREATE TABLE experiments (           -- research registry
  experiment_id TEXT PRIMARY KEY, created TIMESTAMPTZ DEFAULT now(), hypothesis TEXT NOT NULL,
  counterparty TEXT, data_version TEXT, code_hash TEXT, split TEXT, results JSONB, verdict TEXT
);

CREATE TABLE orders (                -- Phase 4+: paper and live share one path
  order_id TEXT PRIMARY KEY, mode TEXT CHECK (mode IN ('paper','live')), asset_id TEXT, side TEXT,
  price NUMERIC, size NUMERIC, tif TEXT, status TEXT, created TIMESTAMPTZ, updated TIMESTAMPTZ,
  strategy TEXT, signal_ref TEXT
);
