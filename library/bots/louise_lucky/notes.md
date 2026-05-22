# louise_lucky

Hereda toda la lógica de `louise` y añade compras adicionales cuando el
precio actual toca un mínimo local en una ventana móvil
(`lucky_window`).

## Tesis

- Cuando hay cash sobrante y la DCA principal no se ha disparado, comprar
  exactamente en un mínimo local mejora el costo promedio sin riesgo
  adicional (sólo agrega cuando ya estamos en posición acumulando).

## Decisiones

- El "techo" para considerar un mínimo es:
  - El `ha_low` de la vela anterior si está disponible, o
  - El mínimo de `price_source` en las últimas `lucky_window` velas.
- Las compras "lucky" no actualizan `last_purchase_price`; así la DCA
  principal sigue evaluando contra el último anclaje "natural".

## Observaciones

- `lucky_window=24` es un buen baseline en intervalos de 1h (~1 día).
- Si el mercado no toca mínimos locales nuevos, el bot se comporta
  idéntico a `louise`.
