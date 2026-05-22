-- Hot-path indexes for the dashboards and the orchestrator's bookkeeping.
-- Keep this file additive: each statement must be safe to re-run via IF NOT EXISTS.

CREATE INDEX IF NOT EXISTS idx_runs_strategy_status
    ON meta.runs (strategy, status);

CREATE INDEX IF NOT EXISTS idx_runs_symbol_interval
    ON meta.runs (symbol, interval);

CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON meta.runs (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_runs_engine_kind
    ON meta.runs (engine_kind);

CREATE INDEX IF NOT EXISTS idx_run_metrics_name
    ON meta.run_metrics (name);

CREATE INDEX IF NOT EXISTS idx_trial_runs_study_state
    ON meta.trial_runs (study_name, state);

CREATE INDEX IF NOT EXISTS idx_trial_runs_run_id
    ON meta.trial_runs (run_id);

CREATE INDEX IF NOT EXISTS idx_trial_runs_objective
    ON meta.trial_runs (study_name, objective_value DESC);

CREATE INDEX IF NOT EXISTS idx_checkpoints_run_simts_desc
    ON meta.checkpoints (run_id, sim_ts DESC);

CREATE INDEX IF NOT EXISTS idx_resource_events_run_ts
    ON ops.resource_events (run_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_run_ts
    ON ops.audit_log (run_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_type
    ON ops.audit_log (event_type);
