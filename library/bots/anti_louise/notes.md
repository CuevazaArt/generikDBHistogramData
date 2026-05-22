# anti_louise

Espejo invertido de `louise` para simular en spot la operativa de una
escalera bajista: las compras representan "anclajes inversos" y el cierre
se produce cuando el precio cae por debajo del precio promedio menos
`target_profit_pct`.

## Tesis

- Si esperamos un colapso desde niveles elevados, acumular en subidas
  controladas y descargar a la primera caída fuerte permite capturar el
  retroceso medio aun sin acceso a short real.

## Decisiones

- Compra inicial cuando hay cash disponible y no hay posición previa.
- Compras sucesivas (`anti_louise_inverse_dca_rise`) cuando el precio
  supera `last_short_anchor * (1 + margin_rise_factor)`.
- Cierre completo (`anti_louise_cover_profit`) cuando el precio cae al
  TP relativo al promedio.

## Observaciones

- Útil como cobertura conceptual cuando se quiere medir performance de
  estrategias "fade-the-rally" en spot.
- Combina con `anti_louise_lucky` para añadir compras en máximos locales.
