# usage-panel

A [Claude Code](https://claude.com/claude-code) mod that opens a pane with your subscription's
**usage limits** (5-hour window, 7-day weekly and Fable weekly): the percentage, a bar, when
each window resets and the **tokens used** in it, together with an estimate of how many it
allows.

```
Session (5h)  42%
████████████████░░░░░░░░░░░░░░░░░░░░░░░░
Resets in 3 h
Used/Expected: 18.4 M / 43.1 M
```

## Install

You need `python3` on your `PATH` (it only uses the standard library).

From the prompt of a Claude Code session in the terminal:

```
/plugin install usage-panel --marketplace Eskladoscla/claude-usage-panel
```

Answer `y` to add the marketplace and pick the scope (user scope, for all your sessions).
From the shell:

```sh
claude plugin install usage-panel --marketplace Eskladoscla/claude-usage-panel
```

In the desktop app: **+** next to the message box → Plugins → Add marketplace with
`Eskladoscla/claude-usage-panel` → Add plugin.

## Usage

Type `/usage-panel` in a session to open the pane. The percentages arrive after the first
turn; the tokens are recalculated at most once per minute.

## How it works

- **Percentages**: every time Claude Code reports your limits and one of them changes, the mod
  appends a line to `~/.claude/usage-ledger/samples/<session>.jsonl`.
- **Tokens**: `scripts/ledger.py` reads your local transcripts (`~/.claude/projects/`)
  incrementally and sums the tokens per window into `~/.claude/usage-ledger/ledger.db`
  (SQLite).
- **Expected**: weighted tokens ÷ the percentage reached in the last window that hit the cap.
  It is an **estimate**: Anthropic does not publish the quota formula, and the weights per
  token type (`~/.claude/usage-ledger/weights.json`, created on first use and editable) are
  taken from the ratio of API prices.
- Usage outside this machine (claude.ai, another computer) raises the percentage without
  leaving tokens here, so the capacity comes out low for those windows.

Everything stays on your machine: the mod uses no network and not your session token, never
blocks anything and, if it fails, it logs to `~/.claude/usage-ledger/errors.log` and the
session carries on.

## Detailed report

From the installed plugin's folder (`--base` changes the data folder):

```sh
python3 -I scripts/ledger.py report --ingest              # every window
python3 -I scripts/ledger.py report --window five_hour    # just one
```

## Tests

```sh
python3 -I -m unittest discover -s tests   # ledger.py
claude plugin test .                       # the mod
```
