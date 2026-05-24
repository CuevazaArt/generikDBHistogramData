# agartha — Binance Alpha (diseño desde cero)

Bot reservado para operar el **mercado Binance Alpha**: tokens en fase temprana,
proyectos semilla, memecoins y activos de **alto riesgo / volatilidad / reputación
moderada**, antes o fuera del spot principal de Binance.

Estado: **WIP** (`reference_only`, sin `entry_point` de backtest aún).
Handoff: [`STATE.md`](STATE.md).

---

## Qué es Binance Alpha (concepto)

Plataforma de **descubrimiento y trading temprano** integrada en el ecosistema
Binance (Exchange + Wallet). No es el order book spot clásico (`BTCUSDT` en
`/api/v3/`).

| Aspecto | Alpha | Spot principal |
|---|---|---|
| Universo | Tokens curados / semilla / pre-listing | Pares listados maduros |
| Riesgo | Alto: baja liquidez, rugs, vol extrema | Relativamente menor |
| Símbolo API | `ALPHA_<id>USDT` (ej. `ALPHA_175USDT`) | `BTCUSDT` |
| Listing | Puede “graduar” a spot; **no garantizado** | Listado formal |
| Order types (Alpha trade) | Documentación indica **LIMIT** principalmente | MARKET, LIMIT, STOP, etc. |

Fuentes: [Binance Alpha docs](https://developers.binance.com/docs/alpha/market-data),
FAQ Alpha 2.0, token list API.

---

## Modelo de símbolos (crítico para Agartha)

1. Llamar **Token List** → obtener `alphaId` + metadata por ticker humano.
2. Construir par de trading: **`ALPHA_<alphaId>USDT`**.

Ejemplo: token `gorilla` con `alphaId: "ALPHA_175"` → par `ALPHA_175USDT`.

En este repo: `BinanceDownloader.resolve_alpha_symbol("GORILLAUSDT")` →
`ALPHA_175USDT` (ver `binance_hist_downloader.py`).

Metadata útil por token (token list):

- `contractAddress`, `chainId`, `chainName` (BSC, etc.)
- `liquidity`, `marketCap`, `fdv`, `holders`
- `listingCex`, `offline`, `offsell`, `canTransfer`
- `listingTime`, `score`, `percentChange24h`, `volume24h`

**Implicación Agartha:** filtro de universo por liquidez/holders/offline antes
de operar.

---

## REST API — Market Data (público)

Base documentada: `https://www.binance.com` (paths `/bapi/defi/v1/public/...`).
Respuesta estándar: `{ code: "000000", success: true, data: ... }`.

| Endpoint | Path | Uso |
|---|---|---|
| **Token List** | `GET /bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list` | Universo + `alphaId` |
| **Exchange Info** | `GET /bapi/defi/v1/public/alpha-trade/get-exchange-info` | Símbolos, filtros, precisiones |
| **Klines** | `GET /bapi/defi/v1/public/alpha-trade/klines` | OHLCV (`symbol`, `interval`, `startTime`, `endTime`, `limit` ≤1500) |

Ya integrado en repo:

```bash
python cli.py --mode alpha_api --symbol GORILLAUSDT --interval 1m ...
```

Intervalos klines (docs): `1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M`.

Otros endpoints públicos (docs Alpha Market Data): agg trades, historial,
volumen — consultar índice en developers.binance.com/docs/alpha.

---

## WebSocket — Market Data

Base WSS (docs): **`wss://nbstream.binance.com/w3w/wsa/stream`**

Streams relevantes para Agartha (símbolo en minúsculas en stream name):

| Stream | Ejemplo | Uso |
|---|---|---|
| `@trade` | `alpha_116usdt@trade` | Prints tick a tick |
| `@aggTrade` | `alpha_116usdt@aggTrade` | Trades agregados |
| `@kline_<interval>` | `alpha_116usdt@kline_1m` | Velas live |
| `@bookTicker` | `alpha_116usdt@bookTicker` | Best bid/ask |
| `@depth` / `@fulldepth` | niveles 5/10/20 | Libro (solo órdenes UI en docs) |
| `@ticker` / `@miniTicker` | 24h stats | Screening |
| `came@allTokens@ticker24` | global | Scanner universo Alpha |

Payload incluye `contractAddress`, chain, volúmenes 24h en algunos tickers globales.

**Implicación Agartha:** latencia y reconexión críticas; memecoins mueven % grandes
en segundos.

---

## Filtros y limitaciones (Exchange Info)

Por símbolo Alpha (ejemplo docs `ALPHA_105USDT`):

| Filtro | Efecto operativo |
|---|---|
| **PRICE_FILTER** | `minPrice`, `maxPrice`, `tickSize` |
| **LOT_SIZE** | `minQty`, `maxQty`, `stepSize` |
| **MIN_NOTIONAL / NOTIONAL** | Notional mín/máx por orden |
| **MAX_NUM_ORDERS** | Tope órdenes abiertas (ej. 200) |
| **PERCENT_PRICE** | `multiplierUp/Down` — precio limitado vs referencia |
| **PERCENT_PRICE_BY_SIDE** | Bid/ask bands (ej. **5× arriba**, **0.2× abajo**) |

Restricciones clave para Agartha:

- **`orderTypes`: `["LIMIT"]`** en Alpha trade — no asumir MARKET como en spot clásico.
- Bandas de precio **muy estrechas hacia abajo** (0.2×): en crash, órdenes limit
  quedan fuera de banda → fills fallidos.
- Precisiones altas (`pricePrecision: 8`) — normalizar con `Decimal`.
- Tokens **`offline: true`** / **`offsell: true`** — excluir del universo.

---

## Trading autenticado (pendiente integrar)

La documentación pública indexada en este repo cubre **market data**. Las órdenes
Alpha trade usan paths `/bapi/defi/v1/...` autenticados (sesión Binance / API wallet),
**distintos** de `POST /api/v3/order` spot clásico.

Agartha debe implementar:

- Descubrir endpoint exacto de **create/cancel/query order** Alpha (docs trade)
- Firma / cookies / headers según producto (Alpha 2.0 en app)
- Idempotencia `clientOrderId`
- Reconciliación fills vía WS user stream (si existe para Alpha)

**No reutilizar** cegado el gateway spot de `imported_bots/.../binance_gateway` sin
validar compatibilidad Alpha.

---

## Perfil de activos objetivo

| Categoría | Características | Riesgo Agartha |
|---|---|---|
| Semilla / pre-listing | Baja liquidez, narrativa fuerte | Slippage, no fill |
| Memecoin | Vol % extremo, pumps/dumps | PERCENT_PRICE, rug |
| Reputación moderada | Pocos holders, contrato opaco | Honeypot, tax |
| “Shitcoin” | Alta emisión, poco utility | Dilución, delist |

Señales del token list para **screening** (no trading aún):

- `liquidity`, `holders`, `volume24h`, `marketCap` vs `fdv`
- `listingCex`, `canTransfer`, `offline`, `offsell`
- `contractAddress` + `chainId` (multi-chain)

---

## Infra existente en este repo

| Componente | Alpha support |
|---|---|
| `binance_hist_downloader.py` | Token list, resolve symbol, klines Alpha |
| `cli.py --mode alpha_api` | Descarga klines → `klines.db` |
| Spot bots (Louise/Dorothy) | **No** — otro universo de símbolos |
| Backtest engine | Reutilizable si hay klines Alpha en DB |

---

## Principios de diseño Agartha (borrador)

1. **Universo dinámico** — refrescar token list; no hardcodear pares.
2. **Filtros duros** — liquidez, offline, min notional, PERCENT_PRICE headroom.
3. **LIMIT-only** — simular/market via limit agresivo + timeout.
4. **Riesgo por evento** — size pequeño; muchos tokens = cartera dispersa.
5. **Salida explícita** — en Alpha no hay “HODL+Earn”; definir stop/time/delist.
6. **Separación total** de Louise/Dorothy — otro mercado, otra filosofía.

---

## Pendientes (próxima sesión)

- [ ] Confirmar docs REST **trade** Alpha (create/cancel/balance)
- [ ] Prototipo connector `alpha_market/` (REST + WS)
- [ ] Estrategia Agartha v0 (tesis + params)
- [ ] Descargar histórico Alpha universo muestra → backtest
- [ ] `entry_point` en manifest cuando exista `AgarthaStrategy`
