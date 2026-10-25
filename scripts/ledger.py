#!/usr/bin/env python3
"""usage-ledger: estima cuántos tokens admite cada ventana de límite de uso de Claude.

Uso:
  python3 -I ledger.py ingest                      # transcripciones + muestras → ledger.db
  python3 -I ledger.py report [--window five_hour|seven_day|fable|<kind>]
                              [--since AAAA-MM-DD] [--md RUTA] [--ingest]

Cruza dos fuentes:
  - muestras de porcentaje que escribe el mod usage-panel en samples/*.jsonl
    (kind, percentUsed, resetsAt), una ventana = (kind, resetsAt);
  - tokens de todas las transcripciones locales de Claude Code
    (~/.claude/projects/*/**/*.jsonl), deduplicados por message.id.

Capacidad estimada = tokens ponderados de la ventana / (% alcanzado / 100). Es una
ESTIMACIÓN: la fórmula real de la cuota no es pública, los pesos son precios
relativos de la API (supuestos editables en weights.json) y lo que se gasta fuera
de este Mac (claude.ai, otro dispositivo) sube el % sin dejar tokens aquí.
Solo usa la biblioteca estándar; ejecutable con `python3 -I`.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
import time

HOME = os.path.expanduser('~')
BASE = os.environ.get('USAGE_LEDGER_DIR') or os.path.join(HOME, '.claude', 'usage-ledger')
PROJECTS = os.path.join(HOME, '.claude', 'projects')
TYPES = ('input', 'output', 'cache_write_5m', 'cache_write_1h', 'cache_read')
ALIASES = {'fable': 'seven_day_overage_included', 'sesion': 'five_hour', 'semanal': 'seven_day'}

DEFAULT_WEIGHTS = {
    '_nota': ('SUPUESTOS editables. "tipos": peso de cada tipo de token en unidades de token de '
              'entrada, según la proporción de precios públicos de la API (salida ×5, escritura de '
              'caché 5 min ×1,25, 1 h ×2, lectura de caché ×0,1). "modelos": factor por modelo '
              '(precio relativo de su entrada frente a la referencia); el primer patrón (regex, sin '
              'distinguir mayúsculas) que casa con el id del modelo manda. Están a 1,0 hasta que se '
              'rellenen con los precios vigentes: solo afectan a las ventanas que mezclan modelos. '
              '"ventanas": duración en horas y patrón de modelos que cuentan en cada kind (null = '
              'todos). Un kind que no esté aquí: 5 h si empieza por five_hour, 168 h si empieza por '
              'seven_day, todos los modelos.'),
    'tipos': {'input': 1.0, 'output': 5.0, 'cache_write_5m': 1.25, 'cache_write_1h': 2.0, 'cache_read': 0.1},
    'modelos': [
        {'patron': 'fable|mythos', 'factor': 1.0},
        {'patron': 'opus', 'factor': 1.0},
        {'patron': 'sonnet', 'factor': 1.0},
        {'patron': 'haiku', 'factor': 1.0},
    ],
    'factor_por_defecto': 1.0,
    'ventanas': {
        'five_hour': {'horas': 5, 'modelos': None},
        'seven_day': {'horas': 168, 'modelos': None},
        'seven_day_overage_included': {'horas': 168, 'modelos': 'fable|mythos'},
        'seven_day_opus': {'horas': 168, 'modelos': 'opus'},
        'seven_day_sonnet': {'horas': 168, 'modelos': 'sonnet'},
    },
    'umbral_estimacion_puntos': 5,
    'umbral_tramo_puntos': 1,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, kind TEXT NOT NULL, size INTEGER, mtime REAL, offset INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (
  key TEXT PRIMARY KEY, ts REAL NOT NULL, project TEXT, session_id TEXT, model TEXT,
  is_subagent INTEGER NOT NULL DEFAULT 0,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
  cache_write_5m INTEGER NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session_id TEXT, cwd TEXT, project TEXT, source TEXT,
  kind TEXT NOT NULL, label TEXT, percent REAL NOT NULL, resets_at TEXT, extra TEXT,
  UNIQUE(session_id, kind, ts, percent, resets_at));
CREATE INDEX IF NOT EXISTS samples_window ON samples(kind, resets_at, ts);
"""

# Los mensajes repetidos (un bloque de contenido por línea) se funden quedándose con el
# máximo de cada contador: nunca se suman.
UPSERT = """
INSERT INTO messages (key, ts, project, session_id, model, is_subagent,
  input, output, cache_write_5m, cache_write_1h, cache_read)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(key) DO UPDATE SET
  ts = min(ts, excluded.ts),
  is_subagent = max(is_subagent, excluded.is_subagent),
  input = max(input, excluded.input), output = max(output, excluded.output),
  cache_write_5m = max(cache_write_5m, excluded.cache_write_5m),
  cache_write_1h = max(cache_write_1h, excluded.cache_write_1h),
  cache_read = max(cache_read, excluded.cache_read)
"""


PLAN_LABELS = {'max_20x': 'Max x20', 'max_5x': 'Max x5', 'pro': 'Pro'}


def current_plan(path=os.path.expanduser('~/.claude.json')):
    """Plan de la cuenta según ~/.claude.json (oauthAccount.*RateLimitTier), sin red; None si no se sabe."""
    try:
        with open(path) as fh:
            acc = json.load(fh).get('oauthAccount') or {}
    except (OSError, ValueError):
        return None
    tier = acc.get('organizationRateLimitTier') or acc.get('userRateLimitTier')
    if not tier:
        return None
    for key, label in PLAN_LABELS.items():
        if key in tier:
            return label
    return tier


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute('CREATE TABLE IF NOT EXISTS caps (kind TEXT NOT NULL, resets_at TEXT NOT NULL, hit_ts REAL NOT NULL, '
               'raw INTEGER NOT NULL, weighted INTEGER NOT NULL, plan TEXT, PRIMARY KEY (kind, resets_at))')
    # plan: el de la muestra si lo trae el mod; si no, el vigente al importarla (plan_src lo distingue).
    for table in ('samples', 'messages'):
        cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'plan' not in cols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN plan TEXT')
            db.execute(f'ALTER TABLE {table} ADD COLUMN plan_src TEXT')
    return db


def parse_ts(s):
    """ISO 8601 (con Z u offset) → epoch en segundos; None si no se entiende."""
    if not s or not isinstance(s, str):
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def norm_reset(s):
    """resetsAt normalizado al minuto en UTC: identifica la ventana aunque cambie el formato."""
    t = parse_ts(s)
    if t is None:
        return None
    return dt.datetime.fromtimestamp(round(t / 60) * 60, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')


def read_new_lines(db, path, kind):
    """Líneas completas añadidas desde la última lectura (offset/mtime en `files`)."""
    st = os.stat(path)
    row = db.execute('SELECT size, mtime, offset FROM files WHERE path=?', (path,)).fetchone()
    offset = 0
    if row:
        size, mtime, offset = row
        if size == st.st_size and mtime == st.st_mtime:
            return []
        if st.st_size < offset:  # fichero reescrito o truncado: se relee (la dedupe lo absorbe)
            offset = 0
    lines = []
    with open(path, 'rb') as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line or not line.endswith(b'\n'):
                break  # una línea a medio escribir se queda para la próxima pasada
            offset += len(line)
            lines.append(line)
    db.execute('INSERT INTO files(path, kind, size, mtime, offset) VALUES (?,?,?,?,?) '
               'ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, offset=excluded.offset',
               (path, kind, st.st_size, st.st_mtime, offset))
    return lines


def message_row(o, project, is_sub):
    if o.get('type') != 'assistant':
        return None
    m = o.get('message') or {}
    u = m.get('usage') or {}
    if not u:
        return None
    key = m.get('id') or o.get('requestId') or o.get('uuid')
    ts = parse_ts(o.get('timestamp'))
    if not key or ts is None:
        return None
    cc = u.get('cache_creation') or {}
    cw_total = int(u.get('cache_creation_input_tokens') or 0)
    cw1h = int(cc.get('ephemeral_1h_input_tokens') or 0)
    cw5m = int(cc.get('ephemeral_5m_input_tokens') or 0)
    cw5m += max(0, cw_total - cw1h - cw5m)  # escritura sin desglose: se cuenta como 5 min
    vals = (int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0), cw5m, cw1h,
            int(u.get('cache_read_input_tokens') or 0))
    if not any(vals):
        return None
    sub = 1 if (is_sub or o.get('isSidechain')) else 0
    return (key, ts, project, o.get('sessionId'), m.get('model') or '?', sub) + vals


def ingest_transcripts(db, projects_dir=PROJECTS):
    files = glob.glob(os.path.join(projects_dir, '*', '**', '*.jsonl'), recursive=True)
    n_lines = n_rows = 0
    for path in files:
        rel = os.path.relpath(path, projects_dir)
        project = rel.split(os.sep, 1)[0]
        is_sub = '/subagents/' in path
        try:
            lines = read_new_lines(db, path, 'transcript')
        except OSError:
            continue
        rows = []
        for line in lines:
            if b'"usage"' not in line or b'"assistant"' not in line:
                continue
            try:
                r = message_row(json.loads(line), project, is_sub)
            except (ValueError, TypeError, AttributeError):
                continue
            if r:
                rows.append(r)
        n_lines += len(lines)
        n_rows += len(rows)
        if rows:
            db.executemany(UPSERT, rows)
            db.execute("UPDATE messages SET plan = ?, plan_src = 'al-importar' WHERE plan IS NULL", (current_plan(),))
        db.commit()
    return {'ficheros': len(files), 'lineas_nuevas': n_lines, 'filas_upsert': n_rows}


def import_samples(db, base=BASE):
    paths = sorted(glob.glob(os.path.join(base, 'samples', '*.jsonl')))
    if os.path.exists(os.path.join(base, 'samples.jsonl')):
        paths.append(os.path.join(base, 'samples.jsonl'))
    n = 0
    for path in paths:
        for line in read_new_lines(db, path, 'samples'):
            try:
                o = json.loads(line)
                ts = parse_ts(o.get('ts'))
                pct = float(o['percentUsed'])
                kind = str(o['kind'])
            except (ValueError, TypeError, KeyError):
                continue
            if ts is None:
                continue
            cur = db.execute('INSERT OR IGNORE INTO samples(ts, session_id, cwd, project, source, kind, label, '
                             'percent, resets_at, extra) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (ts, o.get('sessionId'), o.get('cwd'), o.get('project'), o.get('source'), kind,
                              o.get('label'), pct, norm_reset(o.get('resetsAt')),
                              json.dumps(o['extra']) if o.get('extra') else None))
            n += cur.rowcount
            if cur.rowcount:
                plan, src = (o['plan'], 'muestra') if o.get('plan') else (current_plan(), 'al-importar')
                db.execute('UPDATE samples SET plan = ?, plan_src = ? WHERE id = ?', (plan, src, cur.lastrowid))
        db.commit()
    return n


def load_weights(base=BASE):
    path = os.path.join(base, 'weights.json')
    if not os.path.exists(path):
        os.makedirs(base, exist_ok=True)
        with open(path, 'w') as fh:
            json.dump(DEFAULT_WEIGHTS, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
    with open(path) as fh:
        w = json.load(fh)
    for k, v in DEFAULT_WEIGHTS.items():
        w.setdefault(k, v)
    return w


def model_factor(w, model):
    for m in w['modelos']:
        if re.search(m['patron'], model or '', re.I):
            return float(m['factor'])
    return float(w['factor_por_defecto'])


def window_conf(w, kind):
    c = dict(w['ventanas'].get(kind) or {})
    if 'horas' not in c:
        c['horas'] = 5 if kind.startswith('five_hour') else 168 if kind.startswith('seven_day') else None
    c.setdefault('modelos', None)
    return c


def tokens_between(db, w, t0, t1, models_re, include_t0=True):
    """Tokens por modelo y tipo en [t0, t1] (o (t0, t1]) y su total ponderado."""
    op = '>=' if include_t0 else '>'
    q = (f'SELECT model, sum(input), sum(output), sum(cache_write_5m), sum(cache_write_1h), sum(cache_read) '
         f'FROM messages WHERE ts {op} ? AND ts <= ? GROUP BY model')
    by_model, weighted = {}, 0.0
    for model, *vals in db.execute(q, (t0, t1)):
        if models_re and not re.search(models_re, model or '', re.I):
            continue
        d = dict(zip(TYPES, (int(v or 0) for v in vals)))
        by_model[model] = d
        weighted += model_factor(w, model) * sum(w['tipos'][t] * d[t] for t in TYPES)
    return by_model, weighted


def build_report(db, w, kind=None, since=None):
    kind = ALIASES.get(kind, kind)
    q = 'SELECT DISTINCT kind, resets_at FROM samples WHERE resets_at IS NOT NULL'
    args = []
    if kind:
        q += ' AND kind = ?'
        args.append(kind)
    if since:
        q += ' AND resets_at >= ?'
        args.append(since)
    out = []
    for k, reset in db.execute(q + ' ORDER BY kind, resets_at', args).fetchall():
        conf = window_conf(w, k)
        reset_t = parse_ts(reset)
        samples = db.execute('SELECT ts, percent FROM samples WHERE kind=? AND resets_at=? ORDER BY ts, percent',
                             (k, reset)).fetchall()
        # dentro de una ventana el % solo sube: se usa el máximo acumulado
        mono, top = [], -1.0
        for ts, p in samples:
            top = max(top, p)
            mono.append((ts, top))
        pct = mono[-1][1]
        last_ts = next(ts for ts, p in mono if p == pct)
        row = {'kind': k, 'label': _label(db, k), 'reset': reset_t, 'horas': conf['horas'],
               'modelos': conf['modelos'], 'primera': mono[0][0], 'ultima': mono[-1][0], 'n_muestras': len(mono),
               'pct': pct, 'inicio': None, 'por_modelo': {}, 'ponderados': 0.0, 'capacidad': None,
               'tramos': [], 'mediana': None, 'rango': None, 'residuo': 0}
        if conf['horas'] is None:
            out.append(row)
            continue
        start = reset_t - conf['horas'] * 3600
        row['inicio'] = start
        row['por_modelo'], row['ponderados'] = tokens_between(db, w, start, last_ts, conf['modelos'])
        if pct >= w['umbral_estimacion_puntos'] and pct > 0 and row['ponderados'] > 0:
            row['capacidad'] = row['ponderados'] / (pct / 100.0)
        anchor = mono[0]
        for ts, p in mono[1:]:
            delta = p - anchor[1]
            if delta < w['umbral_tramo_puntos']:
                continue
            _, wt = tokens_between(db, w, anchor[0], ts, conf['modelos'], include_t0=False)
            seg = {'desde': anchor[0], 'hasta': ts, 'delta': delta, 'ponderados': wt,
                   'capacidad': wt / (delta / 100.0) if wt > 0 else None, 'residuo': wt <= 0}
            row['tramos'].append(seg)
            anchor = (ts, p)
        caps = [s['capacidad'] for s in row['tramos'] if s['capacidad']]
        row['residuo'] = sum(1 for s in row['tramos'] if s['residuo'])
        if caps:
            row['mediana'] = statistics.median(caps)
            row['rango'] = (min(caps), max(caps))
        out.append(row)
    return out


def _label(db, kind):
    r = db.execute('SELECT label FROM samples WHERE kind=? AND label IS NOT NULL ORDER BY ts DESC LIMIT 1',
                   (kind,)).fetchone()
    return r[0] if r else kind


def fmt_t(t):
    return dt.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M') if t else '—'


def fmt_n(x):
    if x is None:
        return '—'
    for div, suf in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k')):
        if abs(x) >= div:
            return f'{x / div:.2f}{suf}'.replace('.', ',')
    return f'{x:.0f}'


HEAD = ['Ventana', 'Inicio', 'Reset', '1.ª muestra', 'Última', 'Muestras', '% alcanzado', 'Tok. ponderados',
        'Capacidad est.', 'Tramos', 'Mediana tramos', 'Rango tramos', 'Residuo']


def _cells(r):
    cap = fmt_n(r['capacidad']) if r['capacidad'] else ('< 5 pts' if r['horas'] else 'kind sin duración')
    rango = f"{fmt_n(r['rango'][0])}–{fmt_n(r['rango'][1])}" if r['rango'] else '—'
    return [f"{r['label']} ({r['kind']})", fmt_t(r['inicio']), fmt_t(r['reset']), fmt_t(r['primera']),
            fmt_t(r['ultima']), str(r['n_muestras']), f"{r['pct']:.1f}".replace('.', ','), fmt_n(r['ponderados']),
            cap, str(len(r['tramos'])), fmt_n(r['mediana']), rango,
            f"{r['residuo']} tramo(s): chats u otros dispositivos" if r['residuo'] else '—']


def _detail(r):
    lines = []
    for model, d in sorted(r['por_modelo'].items()):
        lines.append(f"{model}: " + ' · '.join(f"{t} {fmt_n(d[t])}" for t in TYPES))
    for s in r['tramos']:
        if s['residuo']:
            lines.append(f"residuo (chats u otros dispositivos): {fmt_t(s['desde'])} → {fmt_t(s['hasta'])}, "
                         f"+{s['delta']:.1f} pts sin tokens locales")
    return lines


NO_SAMPLES = ('Aún no hay muestras de porcentaje: no se puede estimar la capacidad de ninguna ventana.\n'
              'Activa el mod usage-ledger (README.md, «Activar el mod») y usa Claude Code: cada turno deja '
              'una muestra en samples/. Mientras tanto, tokens locales recientes (todas las sesiones y '
              'proyectos):')


def local_summary(db, w, now=None):
    now = now or time.time()
    lines = []
    for name, hours in (('últimas 5 h', 5), ('últimos 7 días', 168)):
        by_model, wt = tokens_between(db, w, now - hours * 3600, now, None)
        lines.append(f'- {name}: {fmt_n(wt)} ponderados')
        for model, d in sorted(by_model.items()):
            lines.append(f'    {model}: ' + ' · '.join(f'{t} {fmt_n(d[t])}' for t in TYPES))
    return lines


def last_cap(db, w, kind):
    """Tokens gastados en la última ventana de `kind` que llegó al tope (≥100 %), desde su inicio hasta la primera
    muestra al 100 %. Queda registrada en `caps`; None si ninguna ventana llegó al tope."""
    c = window_conf(w, kind)
    for resets_at, hit_ts, plan in db.execute(
            'SELECT resets_at, min(ts), plan FROM samples WHERE kind = ? AND percent >= 100 AND resets_at IS NOT NULL '
            'GROUP BY resets_at ORDER BY resets_at DESC LIMIT 1', (kind,)):
        t_reset = parse_ts(resets_at)
        if not c['horas'] or t_reset is None:
            return None
        by_model, wt = tokens_between(db, w, t_reset - c['horas'] * 3600, hit_ts, c['modelos'])
        raw = sum(sum(d.values()) for d in by_model.values())
        db.execute('INSERT OR REPLACE INTO caps VALUES (?,?,?,?,?,?)', (kind, resets_at, hit_ts, raw, round(wt), plan))
        db.commit()
        return {'raw': raw, 'weighted': round(wt), 'hit_ts': hit_ts, 'plan': plan}
    return None


def window_tokens(db, w, resets, now=None):
    """{kind: {raw, weighted, by_type}} desde el inicio de cada ventana (reset − duración) hasta ahora."""
    now = now or time.time()
    out = {}
    for item in resets:
        kind, _, iso = item.partition('=')
        c = window_conf(w, kind)
        t1 = parse_ts(iso) if iso else None
        if not c['horas'] or t1 is None:
            continue
        by_model, wt = tokens_between(db, w, t1 - c['horas'] * 3600, now, c['modelos'])
        by_type = {t: sum(d[t] for d in by_model.values()) for t in TYPES}
        out[kind] = {'raw': sum(by_type.values()), 'weighted': round(wt), 'by_type': by_type, 'cap': last_cap(db, w, kind)}
    return out


FOOT = ('Estimación, no cifra exacta: capacidad = tokens ponderados / (% / 100), solo con ≥ 5 puntos; '
        'los tramos entre muestras con Δ ≥ 1 punto dan mediana y rango. Pesos en weights.json (supuestos). '
        'Lo gastado fuera de este Mac sube el % sin tokens locales y hace que la capacidad salga a la baja. '
        'Horas en hora local.')


def render_text(rows, db, w):
    if not rows:
        return '\n'.join([NO_SAMPLES] + local_summary(db, w))
    table = [HEAD] + [_cells(r) for r in rows]
    widths = [max(len(c[i]) for c in table) for i in range(len(HEAD))]
    out = []
    for i, c in enumerate(table):
        out.append('  '.join(x.ljust(widths[j]) for j, x in enumerate(c)).rstrip())
        if i == 0:
            out.append('  '.join('-' * x for x in widths))
    for r in rows:
        det = _detail(r)
        if det:
            out.append(f"\n{r['label']} · reset {fmt_t(r['reset'])}:")
            out += ['  ' + x for x in det]
    out.append('\n' + FOOT)
    return '\n'.join(out)


def render_md(rows, db, w):
    out = ['# Informe de límites de uso', '', f'Generado: {fmt_t(time.time())}', '']
    if not rows:
        return '\n'.join(out + [NO_SAMPLES, ''] + local_summary(db, w)) + '\n'
    out.append('| ' + ' | '.join(HEAD) + ' |')
    out.append('|' + '---|' * len(HEAD))
    for r in rows:
        out.append('| ' + ' | '.join(c.replace('|', '/') for c in _cells(r)) + ' |')
    for r in rows:
        det = _detail(r)
        if det:
            out += ['', f"## {r['label']} · reset {fmt_t(r['reset'])}", ''] + [f'- {x}' for x in det]
    return '\n'.join(out + ['', FOOT, ''])


def main(argv=None):
    ap = argparse.ArgumentParser(description='Estima la capacidad en tokens de las ventanas de límite de uso.')
    ap.add_argument('--base', default=BASE, help='carpeta del registro (ledger.db, samples/, weights.json)')
    ap.add_argument('--projects', default=PROJECTS, help='carpeta de transcripciones de Claude Code')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('ingest', help='ingesta incremental de transcripciones y muestras')
    rp = sub.add_parser('report', help='una fila por ventana (kind, reset)')
    rp.add_argument('--window', help='five_hour, seven_day, fable o cualquier kind')
    rp.add_argument('--since', help='solo ventanas con reset desde esta fecha (AAAA-MM-DD)')
    rp.add_argument('--md', help='escribe además el informe en Markdown en esta ruta')
    rp.add_argument('--ingest', action='store_true', help='ingesta las transcripciones antes del informe')
    wp = sub.add_parser('window', help='JSON con los tokens gastados en cada ventana en curso (para el panel del mod)')
    wp.add_argument('resets', nargs='*', help='kind=ISO del reset (resetsAt), uno por ventana')
    wp.add_argument('--ingest', action='store_true', help='ingesta las transcripciones antes de contar')
    a = ap.parse_args(argv)
    db = connect(os.path.join(a.base, 'ledger.db'))
    w = load_weights(a.base)
    if a.cmd == 'window' and a.ingest:
        ingest_transcripts(db, a.projects)
    elif a.cmd == 'ingest' or a.ingest:
        t0 = time.time()
        r = ingest_transcripts(db, a.projects)
        n = import_samples(db, a.base)
        total = db.execute('SELECT count(*) FROM messages').fetchone()[0]
        print(f"Ingesta: {r['ficheros']} ficheros, {r['lineas_nuevas']} líneas nuevas, {r['filas_upsert']} "
              f"filas de uso; {total} mensajes únicos en total; {n} muestras nuevas; {time.time() - t0:.1f} s")
    if a.cmd == 'window':
        import_samples(db, a.base)
        print(json.dumps(window_tokens(db, w, a.resets)))
        return 0
    if a.cmd == 'report':
        if not a.ingest:
            import_samples(db, a.base)
        rows = build_report(db, w, a.window, a.since)
        print(render_text(rows, db, w))
        if a.md:
            with open(a.md, 'w') as fh:
                fh.write(render_md(rows, db, w))
            print(f'\nMarkdown: {a.md}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
