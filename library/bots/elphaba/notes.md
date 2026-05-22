# elphaba

Versión "anti-Dorothy" del adaptador Pecunator: opera la dirección bajista
en un motor spot mediante una DCA inversa con anclajes de "short
simulado".

## Tesis

- En spot largo no se puede vender en corto, pero el bot replica la
  lógica tratando cada compra como un anclaje. Cuando el precio sube por
  encima de `highest_anchor * (1 + profit + margin_rise)` se añade un
  nuevo escalón.
- Cierra (vende) cada anclaje cuando el precio cae por debajo de
  `entry * (1 - profit_factor)`.

## Decisiones

- Sólo opera con `pec_trend == "BEARISH"`. Esto evita acumular anclajes
  caros en tendencia alcista sostenida.
- `max_rungs` limita el número de anclajes simultáneos para acotar la
  exposición.

## Observaciones

- Útil para evaluar performance simétrica frente a `dorothy` en activos
  con regímenes bajistas marcados.
- Alias `elphaba_hub` se mantiene para compatibilidad con runs antiguos.
