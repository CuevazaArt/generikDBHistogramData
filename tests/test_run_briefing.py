"""Tests for pre-run briefing artifacts."""
from backtest.run_briefing import (
    build_run_briefing_payload,
    render_run_briefing_markdown,
    write_run_briefing,
)


def test_run_briefing_renders_gates_and_accessories(tmp_path):
    payload = build_run_briefing_payload(
        study_name="dorothy_xrpusdt_1s_test",
        strategy="dorothy",
        symbol="XRPUSDT",
        interval="1s",
        start_ts=1,
        end_ts=2,
        strategy_params={"profit_factor": 0.02, "max_rungs": 125},
        engine={"fee_rate": 0.001, "events_mode": "lite"},
        execution={"executor": "serial", "windows_label": "2025-01..2025-12"},
        gates={
            "trend_ha_bullish": {
                "active": False,
                "description": "Gate 1 pec_trend == BULLISH",
            },
            "entry_price_below_open": {
                "active": False,
                "description": "Gate 2 pec_entry_gate == BLOCKED",
            },
        },
        accessories={
            "volumen_incremental": {
                "active": True,
                "detail": "multiplier=1.2",
            },
        },
        notes=["Sin gate de tendencia: mas DCA en tramos BEARISH."],
    )
    md = render_run_briefing_markdown(payload)
    assert "DESACTIVO" in md
    assert "volumen_incremental" in md
    assert "ACTIVO" in md

    paths = write_run_briefing(str(tmp_path), payload)
    assert paths["markdown"].endswith("RUN_BRIEFING.md")
    assert paths["json"].endswith("run_briefing.json")
