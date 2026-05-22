# thusnelda

Reserva de namespace y placeholder operativo para Thusnelda. El bot live
existente en `aportes/thusnelda.py` aún no está portado a la API de
`StrategyBase`; el adaptador formal vendrá en una iteración futura.

## Tesis

- Se quiere validar el comportamiento "ducha escocesa" (oscilación entre
  modos conservador/agresivo) sobre datos históricos antes de portarlo al
  motor de backtest.

## Decisiones

- La estrategia devuelve siempre `hold` con `reason="placeholder_*"` para
  poder ser referenciada en pipelines sin alterar métricas.
- El parámetro `placeholder_level` documenta intención sin tener efecto.

## Observaciones

- Mantener este placeholder evita que los reports y registros de
  estrategias rompan referencias a `thusnelda` mientras se trabaja en el
  adaptador real.
- Para la lógica original consulta `library/bots/thusnelda_live_reference`.
