#!/usr/bin/env python3
"""usage-ledger: estimates how many tokens each Claude usage-limit window allows.

Usage:
  python3 -I ledger.py ingest                      # transcripts + samples → ledger.db
  python3 -I ledger.py report [--window five_hour|seven_day|fable|<kind>]
                              [--since YYYY-MM-DD] [--md PATH] [--ingest]

Combines two sources:
  - percentage samples written by the usage-panel mod to samples/*.jsonl
    (kind, percentUsed, resetsAt); one window = (kind, resetsAt);
  - tokens from every local Claude Code transcript
    (~/.claude/projects/*/**/*.jsonl), deduplicated by message.id.

Estimated capacity = weighted tokens in the window / (% reached / 100). It is an
ESTIMATE: the real quota formula is not public, the weights are relative API
prices (editable assumptions in weights.json), and usage outside this Mac
(claude.ai, another device) raises the % without leaving any tokens here.
Standard library only; runs with `python3 -I`.
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
ALIASES = {'fable': 'seven_day_overage_included', 'session': 'five_hour', 'weekly': 'seven_day'}

DEFAULT_WEIGHTS = {
    '_note': ('Editable ASSUMPTIONS. "types": weight of each token type in input-token units, '
              'following the ratio of public API prices (output ×5, cache write 5 min ×1.25, '
              '1 h ×2, cache read ×0.1). "models": factor per model (price of its input relative '
              'to the reference); the first pattern (case-insensitive regex) that matches the model '
              'id wins. They stay at 1.0 until filled in with current prices: they only affect '
              'windows that mix models. "windows": duration in hours and pattern of the models '
              'that count towards each kind (null = all). A kind not listed here: 5 h if it starts '
              'with five_hour, 168 h if it starts with seven_day, all models.'),
    'types': {'input': 1.0, 'output': 5.0, 'cache_write_5m': 1.25, 'cache_write_1h': 2.0, 'cache_read': 0.1},
    'models': [
        {'pattern': 'fable|mythos', 'factor': 1.0},
        {'pattern': 'opus', 'factor': 1.0},
        {'pattern': 'sonnet', 'factor': 1.0},
        {'pattern': 'haiku', 'factor': 1.0},
    ],
    'default_factor': 1.0,
    'windows': {
        'five_hour': {'hours': 5, 'models': None},
        'seven_day': {'hours': 168, 'models': None},
        'seven_day_overage_included': {'hours': 168, 'models': 'fable|mythos'},
        'seven_day_opus': {'hours': 168, 'models': 'opus'},
        'seven_day_sonnet': {'hours': 168, 'models': 'sonnet'},
    },
    'estimate_threshold_points': 5,
    'segment_threshold_points': 1,
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

# Repeated messages (one content block per line) are merged by keeping the maximum of
# each counter: they are never added up.
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
    """Account plan from ~/.claude.json (oauthAccount.*RateLimitTier), no network; None if unknown."""
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
    # plan: the sample's own if the mod provides it; otherwise the one current at import time (plan_src tells them apart).
    for table in ('samples', 'messages'):
        cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'plan' not in cols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN plan TEXT')
            db.execute(f'ALTER TABLE {table} ADD COLUMN plan_src TEXT')
    return db


def parse_ts(s):
    """ISO 8601 (with Z or offset) → epoch seconds; None if it cannot be parsed."""
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
    """resetsAt normalized to the minute in UTC: identifies the window even if the format changes."""
    t = parse_ts(s)
    if t is None:
        return None
    return dt.datetime.fromtimestamp(round(t / 60) * 60, dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')


def read_new_lines(db, path, kind):
    """Complete lines appended since the last read (offset/mtime in `files`)."""
    st = os.stat(path)
    row = db.execute('SELECT size, mtime, offset FROM files WHERE path=?', (path,)).fetchone()
    offset = 0
    if row:
        size, mtime, offset = row
        if size == st.st_size and mtime == st.st_mtime:
            return []
        if st.st_size < offset:  # file rewritten or truncated: read again (dedupe absorbs it)
            offset = 0
    lines = []
    with open(path, 'rb') as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line or not line.endswith(b'\n'):
                break  # a half-written line is left for the next pass
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
    cw5m += max(0, cw_total - cw1h - cw5m)  # cache write without a breakdown: counted as 5 min
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
            db.execute("UPDATE messages SET plan = ?, plan_src = 'at-import' WHERE plan IS NULL", (current_plan(),))
        db.commit()
    return {'files': len(files), 'new_lines': n_lines, 'upserted_rows': n_rows}


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
                plan, src = (o['plan'], 'sample') if o.get('plan') else (current_plan(), 'at-import')
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
    for m in w['models']:
        if re.search(m['pattern'], model or '', re.I):
            return float(m['factor'])
    return float(w['default_factor'])


def window_conf(w, kind):
    c = dict(w['windows'].get(kind) or {})
    if 'hours' not in c:
        c['hours'] = 5 if kind.startswith('five_hour') else 168 if kind.startswith('seven_day') else None
    c.setdefault('models', None)
    return c


def tokens_between(db, w, t0, t1, models_re, include_t0=True):
    """Tokens per model and type in [t0, t1] (or (t0, t1]) and their weighted total."""
    op = '>=' if include_t0 else '>'
    q = (f'SELECT model, sum(input), sum(output), sum(cache_write_5m), sum(cache_write_1h), sum(cache_read) '
         f'FROM messages WHERE ts {op} ? AND ts <= ? GROUP BY model')
    by_model, weighted = {}, 0.0
    for model, *vals in db.execute(q, (t0, t1)):
        if models_re and not re.search(models_re, model or '', re.I):
            continue
        d = dict(zip(TYPES, (int(v or 0) for v in vals)))
        by_model[model] = d
        weighted += model_factor(w, model) * sum(w['types'][t] * d[t] for t in TYPES)
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
        # within a window the % only goes up: use the running maximum
        mono, top = [], -1.0
        for ts, p in samples:
            top = max(top, p)
            mono.append((ts, top))
        pct = mono[-1][1]
        last_ts = next(ts for ts, p in mono if p == pct)
        row = {'kind': k, 'label': _label(db, k), 'reset': reset_t, 'hours': conf['hours'],
               'models': conf['models'], 'first': mono[0][0], 'last': mono[-1][0], 'n_samples': len(mono),
               'pct': pct, 'start': None, 'by_model': {}, 'weighted': 0.0, 'capacity': None,
               'segments': [], 'median': None, 'range': None, 'residual': 0}
        if conf['hours'] is None:
            out.append(row)
            continue
        start = reset_t - conf['hours'] * 3600
        row['start'] = start
        row['by_model'], row['weighted'] = tokens_between(db, w, start, last_ts, conf['models'])
        if pct >= w['estimate_threshold_points'] and pct > 0 and row['weighted'] > 0:
            row['capacity'] = row['weighted'] / (pct / 100.0)
        anchor = mono[0]
        for ts, p in mono[1:]:
            delta = p - anchor[1]
            if delta < w['segment_threshold_points']:
                continue
            _, wt = tokens_between(db, w, anchor[0], ts, conf['models'], include_t0=False)
            seg = {'from': anchor[0], 'to': ts, 'delta': delta, 'weighted': wt,
                   'capacity': wt / (delta / 100.0) if wt > 0 else None, 'residual': wt <= 0}
            row['segments'].append(seg)
            anchor = (ts, p)
        caps = [s['capacity'] for s in row['segments'] if s['capacity']]
        row['residual'] = sum(1 for s in row['segments'] if s['residual'])
        if caps:
            row['median'] = statistics.median(caps)
            row['range'] = (min(caps), max(caps))
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
            return f'{x / div:.2f}{suf}'
    return f'{x:.0f}'


HEAD = ['Window', 'Start', 'Reset', '1st sample', 'Last', 'Samples', '% reached', 'Weighted tok.',
        'Est. capacity', 'Segments', 'Segment median', 'Segment range', 'Residual']


def _cells(r):
    cap = fmt_n(r['capacity']) if r['capacity'] else ('< 5 pts' if r['hours'] else 'kind without duration')
    rng = f"{fmt_n(r['range'][0])}–{fmt_n(r['range'][1])}" if r['range'] else '—'
    return [f"{r['label']} ({r['kind']})", fmt_t(r['start']), fmt_t(r['reset']), fmt_t(r['first']),
            fmt_t(r['last']), str(r['n_samples']), f"{r['pct']:.1f}", fmt_n(r['weighted']),
            cap, str(len(r['segments'])), fmt_n(r['median']), rng,
            f"{r['residual']} segment(s): chats or other devices" if r['residual'] else '—']


def _detail(r):
    lines = []
    for model, d in sorted(r['by_model'].items()):
        lines.append(f"{model}: " + ' · '.join(f"{t} {fmt_n(d[t])}" for t in TYPES))
    for s in r['segments']:
        if s['residual']:
            lines.append(f"residual (chats or other devices): {fmt_t(s['from'])} → {fmt_t(s['to'])}, "
                         f"+{s['delta']:.1f} pts without local tokens")
    return lines


NO_SAMPLES = ('No percentage samples yet, so no window capacity can be estimated.\n'
              'Install the usage-panel mod (see README.md) and use Claude Code: every turn leaves '
              'a sample in samples/. Meanwhile, recent local tokens (all sessions and projects):')


def local_summary(db, w, now=None):
    now = now or time.time()
    lines = []
    for name, hours in (('last 5 h', 5), ('last 7 days', 168)):
        by_model, wt = tokens_between(db, w, now - hours * 3600, now, None)
        lines.append(f'- {name}: {fmt_n(wt)} weighted')
        for model, d in sorted(by_model.items()):
            lines.append(f'    {model}: ' + ' · '.join(f'{t} {fmt_n(d[t])}' for t in TYPES))
    return lines


def last_cap(db, w, kind):
    """Tokens used in the latest `kind` window that hit the cap (≥100 %), from its start to the first sample at
    100 %. Recorded in `caps`; None if no window hit the cap."""
    c = window_conf(w, kind)
    for resets_at, hit_ts, plan in db.execute(
            'SELECT resets_at, min(ts), plan FROM samples WHERE kind = ? AND percent >= 100 AND resets_at IS NOT NULL '
            'GROUP BY resets_at ORDER BY resets_at DESC LIMIT 1', (kind,)):
        t_reset = parse_ts(resets_at)
        if not c['hours'] or t_reset is None:
            return None
        by_model, wt = tokens_between(db, w, t_reset - c['hours'] * 3600, hit_ts, c['models'])
        raw = sum(sum(d.values()) for d in by_model.values())
        db.execute('INSERT OR REPLACE INTO caps VALUES (?,?,?,?,?,?)', (kind, resets_at, hit_ts, raw, round(wt), plan))
        db.commit()
        return {'raw': raw, 'weighted': round(wt), 'hit_ts': hit_ts, 'plan': plan}
    return None


def window_tokens(db, w, resets, now=None):
    """{kind: {raw, weighted, by_type}} from the start of each window (reset − duration) until now."""
    now = now or time.time()
    out = {}
    for item in resets:
        kind, _, iso = item.partition('=')
        c = window_conf(w, kind)
        t1 = parse_ts(iso) if iso else None
        if not c['hours'] or t1 is None:
            continue
        by_model, wt = tokens_between(db, w, t1 - c['hours'] * 3600, now, c['models'])
        by_type = {t: sum(d[t] for d in by_model.values()) for t in TYPES}
        out[kind] = {'raw': sum(by_type.values()), 'weighted': round(wt), 'by_type': by_type, 'cap': last_cap(db, w, kind)}
    return out


FOOT = ('An estimate, not an exact figure: capacity = weighted tokens / (% / 100), only with ≥ 5 points; '
        'segments between samples with Δ ≥ 1 point give the median and range. Weights in weights.json (assumptions). '
        'Usage outside this Mac raises the % without local tokens and makes the capacity come out low. '
        'Times in local time.')


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
    out = ['# Usage limits report', '', f'Generated: {fmt_t(time.time())}', '']
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
    ap = argparse.ArgumentParser(description='Estimates the token capacity of the usage-limit windows.')
    ap.add_argument('--base', default=BASE, help='ledger folder (ledger.db, samples/, weights.json)')
    ap.add_argument('--projects', default=PROJECTS, help='Claude Code transcripts folder')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('ingest', help='incremental ingestion of transcripts and samples')
    rp = sub.add_parser('report', help='one row per window (kind, reset)')
    rp.add_argument('--window', help='five_hour, seven_day, fable or any kind')
    rp.add_argument('--since', help='only windows that reset on or after this date (YYYY-MM-DD)')
    rp.add_argument('--md', help='also write the report as Markdown to this path')
    rp.add_argument('--ingest', action='store_true', help='ingest the transcripts before the report')
    wp = sub.add_parser('window', help='JSON with the tokens used in each current window (for the mod panel)')
    wp.add_argument('resets', nargs='*', help='kind=ISO of the reset (resetsAt), one per window')
    wp.add_argument('--ingest', action='store_true', help='ingest the transcripts before counting')
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
        print(f"Ingest: {r['files']} files, {r['new_lines']} new lines, {r['upserted_rows']} "
              f"usage rows; {total} unique messages in total; {n} new samples; {time.time() - t0:.1f} s")
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
