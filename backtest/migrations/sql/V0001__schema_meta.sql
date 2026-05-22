-- Fase 0 baseline schema for the backtesting framework.
-- Lives in PostgreSQL 16 alongside Optuna's own `optuna` schema (created by Optuna itself).

CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS meta.runs (
    run_id           BIGSERIAL PRIMARY KEY,
    idempotency_key  TEXT UNIQUE NOT NULL,
    strategy         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    interval         TEXT NOT NULL,
    start_ts         BIGINT,
    end_ts           BIGINT,
    initial_cash     DOUBLE PRECISION NOT NULL,
    fee_rate         DOUBLE PRECISION NOT NULL,
    slippage_bps     DOUBLE PRECISION NOT NULL,
    config           JSONB NOT NULL,
    host_info        JSONB,
    status           TEXT NOT NULL,
    engine_kind      TEXT NOT NULL,
    events_parquet   TEXT,
    equity_parquet   TEXT,
    checkpoints_dir  TEXT,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    last_checkpoint  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS meta.run_metrics (
    run_id           BIGINT REFERENCES meta.runs (run_id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    value            DOUBLE PRECISION,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS meta.studies (
    study_name       TEXT PRIMARY KEY,
    strategy         TEXT NOT NULL,
    base_config      JSONB NOT NULL,
    objective_metric TEXT NOT NULL,
    direction        TEXT NOT NULL,
    sampler          TEXT NOT NULL,
    seed             INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meta.trial_runs (
    trial_id         BIGSERIAL PRIMARY KEY,
    study_name       TEXT REFERENCES meta.studies (study_name) ON DELETE CASCADE,
    optuna_trial_num INTEGER NOT NULL,
    run_id           BIGINT REFERENCES meta.runs (run_id),
    params           JSONB NOT NULL,
    objective_value  DOUBLE PRECISION,
    state            TEXT NOT NULL,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    UNIQUE (study_name, optuna_trial_num)
);

-- Auxiliary metric table: required to preserve the legacy `save_trial_metrics`
-- surface exposed by backtest.storage (bt_trial_metrics in SQLite). Without it,
-- per-trial secondary metrics would have no place to live in the PG backend.
CREATE TABLE IF NOT EXISTS meta.trial_metrics (
    trial_id BIGINT REFERENCES meta.trial_runs (trial_id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    value    DOUBLE PRECISION,
    PRIMARY KEY (trial_id, name)
);

CREATE TABLE IF NOT EXISTS meta.checkpoints (
    checkpoint_id    BIGSERIAL PRIMARY KEY,
    run_id           BIGINT REFERENCES meta.runs (run_id) ON DELETE CASCADE,
    sim_ts           BIGINT NOT NULL,
    candle_offset    BIGINT NOT NULL,
    broker_state     JSONB NOT NULL,
    strategy_state   JSONB NOT NULL,
    parquet_path     TEXT NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ops.resource_events (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT,
    ts               TIMESTAMPTZ DEFAULT NOW(),
    event            TEXT NOT NULL,
    snapshot         JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.audit_log (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT,
    ts               TIMESTAMPTZ DEFAULT NOW(),
    event_type       TEXT NOT NULL,
    payload          JSONB
);
