# masha

Bot de tendencia + pullback inspirado en la familia Masha live: combina
un cruce de SMAs con entradas adicionales en retracciones moderadas,
TP/SL fijos por posición.

## Tesis

- En tendencias claras, retroceder un pequeño porcentaje sobre la SMA
  rápida ofrece entradas mejor priceadas que perseguir cruces tardíos.
- Manejar la salida con TP y SL absolutos (en %) reduce dependencia de
  triggers basados en indicadores y simplifica la auditoría.

## Decisiones

- TP y SL se calculan sobre el precio promedio de entrada del broker
  (`ctx.avg_entry`).
- El pullback se mide como porcentaje sobre la SMA rápida actual; si el
  precio toca o cae bajo ese umbral con tendencia activa, dispara compra.
- Cruz bajista (fast<slow) cierra posición incluso antes de tocar TP/SL.

## Observaciones

- Sensible al ratio `fast/slow`; combinaciones inválidas
  (`fast >= slow`) se marcan internamente con `_invalid` para que Optuna
  las descarte.
- En XRPUSDT 1h, presets entre `fast=9 slow=34` y `fast=12 slow=50` han
  mostrado retornos estables en los datos disponibles.
