# ha_trend

Estrategia direccional sobre el cruce de MA(1)/MA(2) calculado en
Heikin-Ashi, con tres modos: ambos (`both`), sólo largos (`long`) o
invertido (`short`) para back-tests de simetría.

## Tesis

- Una rotación de tendencia HA (cambio de `BULLISH` a `BEARISH` o
  viceversa) marca puntos de entrada/salida discretos en mercados con
  micro-estructura ruidosa.

## Decisiones

- En modo `long`: cruz alcista compra; cruz bajista cierra.
- En modo `short` (en spot equivale a "inverso"): cruz bajista compra
  (apostando a la reversión) y cruz alcista cierra.
- `both` es equivalente operativamente a `long` para spot — se conserva
  por compatibilidad con configuraciones antiguas.

## Observaciones

- Combina muy bien con el gate `pec_entry_gate == "CLEAR"` para evitar
  entradas "tarde" en velas alcistas ya extendidas.
- Útil para benchmarking de bots DCA: ofrece una línea base de "trend
  follow" pura sobre el mismo dataset HA.
