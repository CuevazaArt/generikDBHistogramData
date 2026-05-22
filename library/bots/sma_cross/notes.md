# sma_cross

Estrategia base de cruce de medias móviles simples (SMA) usada como
referencia comparativa para los bots más complejos.

## Tesis

- Una SMA rápida (`fast`) atravesando hacia arriba a una SMA lenta (`slow`)
  marca el inicio de una tendencia alcista corta. El cruce contrario cierra
  posición.
- El bot opera siempre long-only en spot: compra en cruz dorada, vende en
  cruz de la muerte. No reabre posiciones intra-vela.

## Decisiones

- Se ignoran las velas previas a `max(fast, slow)` para warm-up y se
  marcan como `hold` (`reason="warmup"`).
- Si los indicadores devuelven `None` (caso borde con datos faltantes), se
  emite `hold` con `reason="indicator_none"` en vez de fallar el run.

## Observaciones

- Útil como benchmark contra estrategias parametrizadas; sus retornos
  base ayudan a detectar regresiones en el motor del backtester.
- Sensible al ratio `fast/slow`; ratios cercanos (e.g. 10/12) generan
  whipsaws caros con fees típicos de 0.1%.
