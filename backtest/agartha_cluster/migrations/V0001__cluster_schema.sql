-- Agartha cluster — schema canónico
-- DB path típico: cluster.db (SQLite, modo WAL)
-- Idempotente: re-ejecutar es safe.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- =============================================================
-- Universo Alpha
-- =============================================================
CREATE TABLE IF NOT EXISTS alpha_universe (
    symbol            TEXT PRIMARY KEY,
    alpha_id          TEXT,
    quote_asset       TEXT,
    listing_ts        INTEGER,
    last_seen_ts      INTEGER,
    status            TEXT NOT NULL DEFAULT 'eligible',
        -- 'eligible' | 'studied' | 'queued' | 'deployed' | 'closed' | 'blacklist' | 'offline'
    holders           INTEGER,
    liquidity_usd     REAL,
    metadata_json     TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_universe_status ON alpha_universe(status);
CREATE INDEX IF NOT EXISTS idx_universe_alpha_id ON alpha_universe(alpha_id);

-- =============================================================
-- Mejores params por símbolo (resultado optuna)
-- =============================================================
CREATE TABLE IF NOT EXISTS symbol_params (
    symbol                   TEXT PRIMARY KEY,
    trailing_stop_pct        REAL NOT NULL,
    activation_profit_pct    REAL NOT NULL,
    breakeven_lock_pct       REAL NOT NULL,
    entry_limit_offset_pct   REAL NOT NULL DEFAULT 0,
    partial_tp_pct           REAL NOT NULL DEFAULT 0,
    partial_tp_size_pct      REAL NOT NULL DEFAULT 0,
    max_holding_bars         INTEGER NOT NULL DEFAULT 0,
    study_equity_pct         REAL,
    study_max_dd_pct         REAL,
    study_trial_id           TEXT,
    optimized_at             TEXT,
    optuna_db_path           TEXT,
    raw_params_json          TEXT,
    FOREIGN KEY (symbol) REFERENCES alpha_universe(symbol)
);

-- =============================================================
-- Exchange filters cacheados (refrescados por REST)
-- =============================================================
CREATE TABLE IF NOT EXISTS symbol_filters (
    symbol                  TEXT PRIMARY KEY,
    tick_size               REAL NOT NULL DEFAULT 1e-8,
    step_size               REAL NOT NULL DEFAULT 1e-8,
    min_notional            REAL NOT NULL DEFAULT 0.1,
    bid_multiplier_up       REAL NOT NULL DEFAULT 5.0,
    bid_multiplier_down     REAL NOT NULL DEFAULT 0.2,
    ask_multiplier_up       REAL NOT NULL DEFAULT 5.0,
    ask_multiplier_down     REAL NOT NULL DEFAULT 0.2,
    refreshed_at            TEXT,
    raw_filters_json        TEXT
);

-- =============================================================
-- Cluster bots (una fila por instancia)
-- =============================================================
CREATE TABLE IF NOT EXISTS cluster_bots (
    bot_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    state                   TEXT NOT NULL,
    capital_usdt            REAL NOT NULL DEFAULT 10,
    params_snapshot_json    TEXT NOT NULL,
    correlation_id          TEXT NOT NULL,
    entry_order_id          TEXT,
    entry_client_order_id   TEXT,
    entry_price             REAL,
    entry_qty               REAL,
    entry_filled_ts         INTEGER,
    peak_price              REAL,
    trail_floor             REAL,
    exit_order_id           TEXT,
    exit_client_order_id    TEXT,
    exit_price              REAL,
    exit_qty                REAL,
    exit_filled_ts          INTEGER,
    realized_pnl_usdt       REAL,
    deployed_at             TEXT,
    closed_at               TEXT,
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bots_state ON cluster_bots(state);
CREATE INDEX IF NOT EXISTS idx_bots_symbol ON cluster_bots(symbol);
CREATE INDEX IF NOT EXISTS idx_bots_correlation ON cluster_bots(correlation_id);

-- =============================================================
-- Historial de transiciones de estado por bot
-- =============================================================
CREATE TABLE IF NOT EXISTS bot_state_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          INTEGER NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    reason          TEXT,
    ts_ms           INTEGER NOT NULL,
    correlation_id  TEXT,
    FOREIGN KEY (bot_id) REFERENCES cluster_bots(bot_id)
);
CREATE INDEX IF NOT EXISTS idx_state_log_bot ON bot_state_log(bot_id, ts_ms);

-- =============================================================
-- Deploy queue (planificación 1 cada 10 min)
-- =============================================================
CREATE TABLE IF NOT EXISTS deploy_queue (
    queue_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    planned_deploy_ts   INTEGER NOT NULL,
    actual_deploy_ts    INTEGER,
    priority            INTEGER NOT NULL DEFAULT 100,
    status              TEXT NOT NULL DEFAULT 'planned',
        -- 'planned' | 'optimizing' | 'ready' | 'deployed' | 'skipped' | 'failed'
    bot_id              INTEGER,
    reason              TEXT,
    enqueued_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON deploy_queue(status, planned_deploy_ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_queue_active_symbol
    ON deploy_queue(symbol) WHERE status IN ('planned','optimizing','ready');

-- =============================================================
-- Órdenes (entry + exit + cancels + reorders)
-- =============================================================
CREATE TABLE IF NOT EXISTS orders (
    order_pk            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            TEXT,
    client_order_id     TEXT NOT NULL,
    bot_id              INTEGER NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    order_type          TEXT NOT NULL,
    state               TEXT NOT NULL,
    price               REAL NOT NULL,
    qty                 REAL NOT NULL,
    filled_qty          REAL NOT NULL DEFAULT 0,
    avg_fill_price      REAL NOT NULL DEFAULT 0,
    submitted_ts        INTEGER,
    last_update_ts      INTEGER,
    correlation_id      TEXT,
    raw_response        TEXT,
    FOREIGN KEY (bot_id) REFERENCES cluster_bots(bot_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_bot ON orders(bot_id);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_orders_client_id ON orders(client_order_id);

-- =============================================================
-- Fills (recibidos desde userDataStream)
-- =============================================================
CREATE TABLE IF NOT EXISTS fills (
    fill_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id              INTEGER NOT NULL,
    order_pk            INTEGER,
    exchange_fill_id    TEXT,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    price               REAL NOT NULL,
    qty                 REAL NOT NULL,
    fee                 REAL NOT NULL DEFAULT 0,
    fee_asset           TEXT,
    ts_ms               INTEGER NOT NULL,
    is_maker            INTEGER NOT NULL DEFAULT 0,
    correlation_id      TEXT,
    raw_payload         TEXT,
    FOREIGN KEY (bot_id)   REFERENCES cluster_bots(bot_id),
    FOREIGN KEY (order_pk) REFERENCES orders(order_pk)
);
CREATE INDEX IF NOT EXISTS idx_fills_bot ON fills(bot_id);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_pk);

-- =============================================================
-- API calls log (REST)
-- =============================================================
CREATE TABLE IF NOT EXISTS api_calls (
    call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    endpoint        TEXT NOT NULL,
    method          TEXT NOT NULL,
    weight          INTEGER NOT NULL DEFAULT 0,
    status_code     INTEGER,
    latency_ms      INTEGER,
    bot_id          INTEGER,
    correlation_id  TEXT,
    request_summary TEXT,
    error_text      TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts_ms);
CREATE INDEX IF NOT EXISTS idx_api_calls_bot ON api_calls(bot_id);

-- =============================================================
-- Event log estructurado (servicio + API + supervisor)
-- =============================================================
CREATE TABLE IF NOT EXISTS event_log (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    source          TEXT NOT NULL,
    level           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    bot_id          INTEGER,
    symbol          TEXT,
    correlation_id  TEXT,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts_ms);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event_log(kind);
CREATE INDEX IF NOT EXISTS idx_event_bot ON event_log(bot_id);
CREATE INDEX IF NOT EXISTS idx_event_level ON event_log(level);

-- =============================================================
-- API throttle rolling buckets (minute-grained weight / orders)
-- =============================================================
CREATE TABLE IF NOT EXISTS api_throttle_buckets (
    minute_bucket   INTEGER PRIMARY KEY,
    weight_used     INTEGER NOT NULL DEFAULT 0,
    orders_count    INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =============================================================
-- Reconciliation snapshots (periódicos)
-- =============================================================
CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
    snap_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms               INTEGER NOT NULL,
    open_orders_count   INTEGER,
    positions_count     INTEGER,
    total_equity_usdt   REAL,
    drift_detected      INTEGER NOT NULL DEFAULT 0,
    snapshot_json       TEXT
);

-- =============================================================
-- Service runs (lifecycle)
-- =============================================================
CREATE TABLE IF NOT EXISTS service_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    stopped_at  TEXT,
    mode        TEXT NOT NULL,   -- 'live' | 'dry-run'
    pid         INTEGER,
    host        TEXT,
    version     TEXT,
    stop_reason TEXT
);

-- =============================================================
-- Credentials meta (PUNTERO; nunca el secreto)
-- =============================================================
CREATE TABLE IF NOT EXISTS credentials_meta (
    profile         TEXT PRIMARY KEY,    -- ej. 'default'
    service_name    TEXT NOT NULL,       -- ej. 'binance_alpha'
    username        TEXT NOT NULL,       -- username del keyring
    storage_method  TEXT NOT NULL,       -- 'os_keyring' | 'env_file'
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
);

-- =============================================================
-- Schema version
-- =============================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '1');
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_applied_at', datetime('now'));
