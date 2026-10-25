# usage-panel

Mod de [Claude Code](https://claude.com/claude-code) que abre un panel con los **límites de
uso** de tu suscripción (ventana de 5 h, semanal de 7 días y semanal de Fable): el
porcentaje, una barra, cuándo se reinicia cada ventana y los **tokens gastados** en ella,
junto a una estimación de cuántos admite.

```
Sesión (5 h)  42%
████████████████░░░░░░░░░░░░░░░░░░░░░░░░
Se reinicia en 3 h
Gastados/Previstos: 18,4 M / 43,1 M
```

## Instalar

Necesitas `python3` en el `PATH` (solo usa la biblioteca estándar).

En la línea de comandos de una sesión de Claude Code en el terminal:

```
/plugin install usage-panel --marketplace Eskladoscla/claude-usage-panel
```

Responde `y` para añadir el marketplace y elige el ámbito (el de usuario, para todas tus
sesiones). Desde la shell:

```sh
claude plugin install usage-panel --marketplace Eskladoscla/claude-usage-panel
```

En la app de escritorio: **+** junto a la caja del mensaje → Plugins → Add marketplace con
`Eskladoscla/claude-usage-panel` → Add plugin.

## Uso

Escribe `/usage-panel` en una sesión para abrir el panel. Los porcentajes llegan tras el
primer turno; los tokens se recalculan como mucho una vez por minuto.

## Cómo funciona

- **Porcentajes**: cada vez que Claude Code informa de tus límites y alguno cambia, el mod
  guarda una línea en `~/.claude/usage-ledger/samples/<sesión>.jsonl`.
- **Tokens**: `scripts/ledger.py` lee de forma incremental tus transcripciones locales
  (`~/.claude/projects/`) y suma los tokens por ventana en `~/.claude/usage-ledger/ledger.db`
  (SQLite).
- **Previstos**: tokens ponderados ÷ porcentaje alcanzado en la última ventana que llegó al
  tope. Es una **estimación**: Anthropic no publica la fórmula de la cuota, y los pesos por
  tipo de token (`~/.claude/usage-ledger/weights.json`, se crea al primer uso y se puede
  editar) se toman de la proporción de precios de la API.
- Lo que gastas fuera de esta máquina (claude.ai, otro ordenador) sube el porcentaje sin
  dejar tokens aquí, así que la capacidad sale a la baja en esas ventanas.

Todo se queda en tu máquina: el mod no usa red ni tu token de sesión, nunca bloquea nada y,
si falla, lo anota en `~/.claude/usage-ledger/errors.log` y la sesión sigue.

## Informe detallado

Desde la carpeta del plugin instalado (`--base` cambia la carpeta de datos):

```sh
python3 -I scripts/ledger.py report --ingest              # todas las ventanas
python3 -I scripts/ledger.py report --window five_hour    # una sola
```

## Tests

```sh
python3 -I -m unittest discover -s tests   # ledger.py
claude plugin test .                       # el mod
```
