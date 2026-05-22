# dorothy

Adaptador de backtest para Pecunator Dorothy: DCA con escalera de profit
operando exclusivamente cuando el gate de tendencia HA está BULLISH.

## Tesis

- En spot long-only, comprar de a tramos cuando el precio cae por debajo
  de un anclaje configurable y descargar mediante límites de venta cuando
  el precio alcanza objetivos de profit acumulado.
- Filtrar entradas con `pec_trend == "BULLISH"` reduce DCAs prolongados
  durante mercados bajistas extendidos.

## Decisiones

- `profit_factor`: ganancia objetivo por límite de venta (se compone con
  la caída adicional `margin_drop_factor` para gatillar la siguiente
  compra).
- `max_rungs`: número máximo de niveles activos simultáneos. Detiene
  nuevas compras cuando se alcanza para limitar exposición.
- Las salidas son solo por límites: un trigger `sell_limit_hit` empuja la
  proporción de tramos cubiertos.
- El estado serializable (`active_sell_limits`) habilita warm restarts
  entre runs sin perder los anclajes activos.

## Observaciones

- Sensible al spread `profit_factor + margin_drop_factor`. Valores muy
  cercanos a 0 producen frecuencia altísima de compras con baja prima por
  tramo (impacto de fees).
- En `XRPUSDT 1h` el sweet spot histórico oscila en
  `profit_factor∈[0.04, 0.06]` y `margin_drop_factor∈[0.005, 0.015]`.
- Alias `dorothy_hub` se mantiene por compatibilidad con scripts
  anteriores.
