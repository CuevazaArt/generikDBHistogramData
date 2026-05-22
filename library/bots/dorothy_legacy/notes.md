# dorothy_legacy

Adaptador original que replica el comportamiento de la primera generación
de Dorothy live (antes del gate de tendencia HA). Se conserva por motivos
históricos y para regresiones contra setups antiguos.

## Tesis

- Igual idea de DCA escalonada que `dorothy`, pero sin filtro de tendencia.
  Esto permite operar en cualquier régimen y, a cambio, exige un techo
  estricto de órdenes activas para evitar reventar el book.

## Decisiones

- Restringe el tamaño de operación al intervalo `[min_order_notional,
  max_order_notional]` y rechaza compras con
  `insufficient_notional` cuando el cash disponible es menor que el mínimo.
- Cap máximo de órdenes activas configurable (`max_active_orders`, default
  200) para evitar acumulación incontrolada en mercados bajistas largos.

## Observaciones

- Para nuevas búsquedas se recomienda usar `dorothy` (con gate HA). Esta
  versión queda como referencia legacy y test de regresión.
- El sweet spot histórico de profit/drop es más estrecho que en `dorothy`
  porque opera continuamente sin filtro.
