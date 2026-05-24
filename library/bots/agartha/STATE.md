# Estado de sesión — handoff

Actualizado: 2026-05-24 (UTC)

## Completado (Louise / Lucky)

- Instrumento **Louise HODL+Earn**: preset `library/bots/louise/presets/hodl_earn_accumulate.yaml`
- Corridas encadenadas 2024→2026 sin TP (`mdf=0.04`, `loop=29`): ETH, XRP, BTC, BNB
  - XRP alcanzó +200 USDT en nov 2024 (+230 total)
  - Entregables en `reports/entregables/strict/louise_*_chain_*`
- **Lucky** redefinido como bot **especialista independiente** (evento: mínimo local)
- Scripts: `scripts/run_louise_ethusdt_pilot.py`, `scripts/run_louise_multi_compare.py`
- Código: `target_profit_pct <= 0` desactiva TP; Louise `export_state` para cadena mensual

## Tags recientes

- `v2026.05.24-louise-lucky-hodl` — pilot scripts + HODL instrument
- `004e09c` — Lucky specialist identity (manifest + notes)

## Agartha — siguiente sesión

1. Definir tesis de trading (scalp / swing / sniper listing / exit rules)
2. Adapter REST Alpha trade (órdenes autenticadas) + WS market data
3. Normalizar símbolos `ALPHA_<id>USDT` y filtros exchange
4. Backtest con `cli.py --mode alpha_api` + datos en `klines.db`
5. Reglas de riesgo: liquidez mínima, PERCENT_PRICE, delisting/offline

Ver [`notes.md`](notes.md) — investigación Binance Alpha API.
