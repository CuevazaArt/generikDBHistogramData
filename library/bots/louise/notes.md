# louise

Estrategia DCA en bajada con cierre completo al alcanzar un porcentaje
objetivo sobre el precio promedio de entrada.

## Tesis

- En activos con drawdowns frecuentes pero retornos positivos, una DCA
  controlada por `margin_drop_factor` baja el costo promedio rápido y
  permite cerrar al primer rebote modesto (`target_profit_pct`).

## Decisiones

- Sale 100% de la posición en cuanto el precio cruza el TP. No usa
  ladders de venta como Dorothy.
- Compra inicial cuando hay cash disponible y no hay posición previa
  o no se conoce el último precio de compra.
- Compras sucesivas (`louise_dca_drop`) sólo cuando el precio cae más
  allá de `last_purchase_price * (1 - margin_drop_factor)`.

## Observaciones

- Sensible a `target_profit_pct` bajos (<0.5): muchísimas operaciones
  pequeñas, fees comen el retorno.
- Funciona bien como bloque base de la familia `louise_lucky` que añade
  entradas oportunistas en mínimos locales.
