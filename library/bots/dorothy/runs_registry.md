# Registro comparativo — Dorothy XRPUSDT 1s (strict monthly chain)

Actualizado: 2026-05-23. Gates **desactivados por defecto** desde este registro
(`--require-trend-gate` / `--require-entry-gate` para activar).

Base común de reproducción (salvo ventana y flags indicados):

```powershell
$env:PYTHONPATH='.'
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --symbol XRPUSDT --interval 1s `
  --start_ts <START_TS> --end_ts <END_TS> `
  --initial_cash 1000 --loop_seconds 29 `
  --margin_drop_factor 0.0005 --profit_factor_grid "0.02" `
  --fee_rate 0.001 --slippage_bps 2 `
  --cpu_cap_pct 90 --db klines.db
```

## Tabla comparativa (seteos y resultados)

| ID estudio | Ventana (UTC ms) | Año | Gate 1 trend | Gate 2 entry | Accesorio | Params accesorio | final_equity | Retorno | git | Carpeta |
|---|---|---|---|---|---|---|---:|---:|---|---|
| `...011611` | 1704067200000–1735689599000 | 2024 | ON (legacy default) | OFF | VI | mult 1.2 | 1768.82 | +76.9 % | 2ed30c5 | `...011611/` |
| `...013946` | 1704067200000–1735689599000 | 2024 | ON | OFF | — | — | 1768.82 | +76.9 % | 2ed30c5 | `...013946/` |
| `...023956` | 1704067200000–1735689599000 | 2024 | ON | OFF | VI | mult 1.35 | 1707.45 | +70.7 % | 2ed30c5 | `...023956/` |
| `...045039` | 1704067200000–1735689599000 | 2024 | OFF (`--no-trend-gate`) | OFF | VI | mult 1.2 | 1753.86 | +75.4 % | 51865bc | `...045039/` |
| `...151440` | 1704067200000–1735689599000 | 2024 | ON (`--require-trend-gate`) | OFF | **VC** | min 6 USDT, greed 0 | 1840.97 | +84.1 % | a76f03c | `...151440/` |
| `...152558` | 1704067200000–1735689599000 | 2024 | **OFF (default)** | OFF | **VC** | min 6 USDT, greed 0 | 1990.60 | +99.1 % | local* | `...152558/` |
| `...154210` | 1704067200000–1735689599000 | 2024 | OFF | OFF | **VC** | min 6 USDT, **greed 0.01** | 2002.98 | +100.3 % | local* | `...154210/` |
| `...155814` | 1704067200000–1735689599000 | 2024 | OFF | OFF | **VC** | min 6 USDT, **greed 0.1** | **2021.67** | **+102.2 %** | local* | `...155814/` |
| `...041134` | 1735689600000–1767225599000 | 2025 | ON | OFF | VI | mult 1.35 | 940.28 | −6.0 % | 51865bc | `...041134/` |
| `...050152` | 1735689600000–1767225599000 | 2025 | OFF | OFF | VI | mult 1.2 | 927.16 | −7.3 % | a76f03c | `...050152/` |
| `...161542` | 1735689600000–1767225599000 | 2025 | **OFF (default)** | OFF | **VC** | min 6 USDT, greed 0 | 848.62 | −15.1 % | local* | `...161542/` |
| `...162111` | 1735689600000–1767225599000 | 2025 | ON (`--require-trend-gate`) | OFF | **VC** | min 6 USDT, greed 0 | 849.83 | −15.0 % | local* | `...162111/` |
| `...171322` | 1735689600000–1779516000000 | 2025–2026 | OFF | OFF | **VC** | min 6, greed 0.1 | 580.06 | −42.0 % | local* | `...171322/` |
| `...175542` | 1704067200000–1779516000000 | 2024–2026 | OFF | OFF | **VC** | min 6, greed 0.1, pf 0.02 | 1349.61 | +35.0 % | local* | `...175542/` |
| `...182233` | 1704067200000–1779516000000 | 2024–2026 | OFF | OFF | **VC** | min 6, greed 0.1, pf 0.1 | 1139.16 | +13.9 % | local* | `...182233/` |
| `...184952` | 1704067200000–1779516000000 | 2024–2026 | OFF | OFF | **VC** | min 6, greed 0.1, pf 0.1 | 1395.80 | +39.6 % | local* | `...184952/` |
| `...200251` | 1704067200000–1779516000000 | 2024–2026 | OFF | OFF | **VC** | min 6, greed 0.1, pf 0.03 | 1415.15 | +41.5 % | local* | `...200251/` |
| `...200250` | 1704067200000–1779516000000 | 2024–2026 | OFF | OFF | **VC** | min 6, greed 0.1, pf 0.03 | 948.46 | −5.2 % | local* | `...200250/` |

\* Corridas VC/greed locales antes de commit; código incluye VC + `GreedFactor` + gates off por defecto.

Rutas completas bajo `reports/entregables/strict/dorothy_xrpusdt_1s_monthly_chain_20260523_<tag>/`.

## Cambios de lógica / seteos (contexto por corrida)

| Cambio | Corridas afectadas | Qué difiere |
|---|---|---|
| Baseline histórico | `023956`, `011611` | VI activo; gate 1 trend **ON** por default legacy; pf=0.02 (011611 usó pf=0.03 en grid) |
| Gates OFF explícito | `045039`, `050152` | `--no-trend-gate` / nuevo default; sells antes del gate; más entradas en meses bajistas |
| Default gates OFF en código | `152558`+ | Sin flag: `require_trend_gate=False`; opt-in con `--require-trend-gate` |
| Accesorio **VolumenCompuesto** (VC) | `151440`, `152558`, `154210`, `155814` | Reemplaza VI; sizing `max(min_usdt, 8×factor)` con Decimal; VI/VC mutuamente excluyentes |
| VC + gate ON | `151440` | Mismo VC pero trend gate activo → menos equity que VC+OFF |
| **GreedFactor** en VC | `154210`, `155814` | `factor = (equity/initial) × (1 + greed)`; amplifica notional cuando equity crece |
| Sweep greed 0.01 vs 0.1 | `154210` vs `155814` | Mismo stack VC+OFF; solo sube `--volumen-compuesto-greed-factor` |
| Ventana 2025 | `041134`, `050152`, `161542`, `162111` | Mismos seteos VI/VC que 2024 pero año distinto (mercado lateral/bajista XRP) |
| VC en 2025 (validación) | `161542`, `162111` | VC pierde ~80–90 USDT vs VI; gate ON/OFF casi indiferente (−15 %) |
| Multi-símbolo 2024–2026 | `175542`, `182233`, `184952`, `200251`, `200250` | Mismo stack VC+greed; pf calibrado por par; ver `notes.md` |
| Filosofía HODL+Earn | todas las encadenadas | Bag underwater → Earn; no reset; BE en checkpoints anuales |

## Comandos de reproducción por fila

### 2024 — VI 1.35, gates ON (baseline histórico `...023956`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-incremental --volumen-incremental-multiplier 1.35 `
  --require-trend-gate
```

### 2024 — VI 1.2, gates OFF (`...045039`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-incremental --volumen-incremental-multiplier 1.2
```

### 2024 — VolumenCompuesto, gates ON (`...151440`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6 `
  --require-trend-gate
```

### 2024 — VolumenCompuesto, gates OFF (`...152558`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6
```

### 2024 — VC + greed 0.01 (`...154210`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6 `
  --volumen-compuesto-greed-factor 0.01
```

### 2024 — VC + greed 0.1 (`...155814`) ★ mejor equity

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6 `
  --volumen-compuesto-greed-factor 0.1
```

### 2025 — VI 1.35, gates ON (`...041134`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1735689600000 --end_ts 1767225599000 `
  --volumen-incremental --volumen-incremental-multiplier 1.35 `
  --require-trend-gate
```

### 2025 — VI 1.2, gates OFF (`...050152`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1735689600000 --end_ts 1767225599000 `
  --volumen-incremental --volumen-incremental-multiplier 1.2
```

### 2025 — VolumenCompuesto, gates OFF (`...161542`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1735689600000 --end_ts 1767225599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6
```

### 2025 — VolumenCompuesto, gates ON (`...162111`)

```powershell
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --start_ts 1735689600000 --end_ts 1767225599000 `
  --volumen-compuesto --volumen-compuesto-min-usdt 6 `
  --require-trend-gate
```

## Parámetros fijos (todas las filas salvo indicado)

| Parámetro | Valor |
|---|---|
| `symbol`   / `interval` | XRPUSDT / 1s |
| `initial_cash` | 1000 USDT |
| `profit_factor` | 0.02 |
| `margin_drop_factor` | 0.0005 |
| `quote_order_qty_usdt` | 8 |
| `max_rungs` | 125 (min(200, cash/8)) |
| `loop_seconds` | 29 |
| `fee_rate` / `slippage_bps` | 0.001 / 2 |
| `events_mode` | lite (`snapshot_seconds=3600`) |
| Modo | `--chain-by-month` (12 ventanas YYYY-MM) |
| `db` | `klines.db` |

## Fórmula VolumenCompuesto + GreedFactor

```text
VC_factor = (equity / initial_equity) × (1 + greed_factor)
notional  = max(min_usdt, 8 × VC_factor)
```

## Artefactos por corrida

Cada carpeta bajo `reports/entregables/strict/<study_name>/`:

- `RUN_BRIEFING.md` + `run_briefing.json` — pre-run (directiva #8)
- `run_manifest.json` — resultado + `code_version` git
- `RESTART_LOG.md` — equity mensual y estado broker/limits
- `MANIFEST.md`

## Lecturas rápidas

1. **VC + gates OFF + greed 0.1** lidera 2024 (**2022 USDT**); greed escala: 0 → 1991, 0.01 → 2003, 0.1 → 2022.
2. En **2025**, **VI supera VC** (~940 vs ~849 USDT); VC empeora ~9 pp vs VI independiente del gate.
3. Gates ON protegen levemente en 2025 con VI; con VC gate ON/OFF es casi igual (−15 %).
4. VI y VC son **mutuamente excluyentes**; no combinar en una misma corrida.
5. Seteo base paradigmático: VC + greed 0.1 + gates OFF; **pf por activo** (BTC 0.03, XRP 0.1, etc.).
6. Datos históricos en `klines.db` + Parquet son **persistentes y reutilizables** (directiva #9).
