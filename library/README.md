# library/ — Biblioteca de bots, indicadores y herramientas

Esta carpeta agrupa, de forma discreta y autocontenida, cada artefacto
"vivo" del proyecto (bots, indicadores, tools). La idea es que un nuevo
colaborador pueda abrir una sola carpeta y entender qué hace una pieza,
con qué parámetros opera por defecto y cuál fue su historia.

## Estructura

```
library/
  bots/<nombre>/
    manifest.yaml         # metadatos + parámetros default + search space
    strategy.py           # opcional cuando entry_point apunta a otro módulo
    notes.md              # tesis, decisiones, observaciones
    presets/default.yaml  # presets nombrados de parámetros
    examples/             # invocaciones CLI de ejemplo
    tests/                # tests específicos del bot
  indicators/<nombre>/    # mismas convenciones, expone compute()
  tools/<nombre>/         # expone run() o class Tool
  workspace/              # borradores en curso; NO se auto-registran
  _index.json             # autogenerado por `library refresh`
```

Cada entrada se identifica por su carpeta y por el campo `name` del
`manifest.yaml`. El motor de la biblioteca (`backtest/library.py`) se
encarga de listar, validar, escalar (scaffold), publicar y registrar
las entradas en `backtest.registry` para que el CLI las reconozca.

## Uso rápido

```bash
python backtest_cli.py library list
python backtest_cli.py library show dorothy
python backtest_cli.py library new mi_bot --kind bot
python backtest_cli.py library validate mi_bot --workspace
python backtest_cli.py library publish mi_bot
python backtest_cli.py library refresh
```

Para una guía completa consulta [`docs/LIBRARY.md`](../docs/LIBRARY.md).
