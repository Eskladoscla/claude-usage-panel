"""Fast tests for ledger.py with synthetic fixtures: python3 -I -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import ledger  # noqa: E402


def asst(mid, ts, model, rid='req_1', **u):
    usage = {'input_tokens': u.get('inp', 0), 'output_tokens': u.get('out', 0),
             'cache_creation_input_tokens': u.get('cw5', 0) + u.get('cw1', 0),
             'cache_creation': {'ephemeral_5m_input_tokens': u.get('cw5', 0),
                                'ephemeral_1h_input_tokens': u.get('cw1', 0)},
             'cache_read_input_tokens': u.get('cr', 0)}
    return json.dumps({'type': 'assistant', 'timestamp': ts, 'requestId': rid, 'sessionId': 's1',
                       'message': {'id': mid, 'model': model, 'usage': usage}})


def sample(ts, kind, pct, reset, sid='s1'):
    return json.dumps({'ts': ts, 'sessionId': sid, 'cwd': '/w/p', 'project': 'p', 'source': 'measure',
                       'kind': kind, 'label': kind, 'percentUsed': pct, 'resetsAt': reset})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, 'ledger')
        self.projects = os.path.join(self.tmp.name, 'projects')
        os.makedirs(os.path.join(self.projects, 'proj-a', 'sess', 'subagents'))
        os.makedirs(os.path.join(self.base, 'samples'))
        self.db = ledger.connect(os.path.join(self.base, 'ledger.db'))
        self.w = ledger.load_weights(self.base)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def write(self, rel, lines, mode='w', newline=True):
        with open(os.path.join(self.projects, rel), mode) as fh:
            fh.write('\n'.join(lines) + ('\n' if newline else ''))


class Dedupe(Base):
    def test_same_message_id_on_several_lines_counts_once(self):
        # three blocks of the same message: the final cumulative output is 50, not 10+10+50
        self.write('proj-a/sess.jsonl', [
            asst('msg_A', '2026-10-25T10:30:00Z', 'claude-opus-5-5', inp=100, out=10, cr=1000),
            asst('msg_A', '2026-10-25T10:30:01Z', 'claude-opus-5-5', inp=100, out=10, cr=1000),
            asst('msg_A', '2026-10-25T10:30:02Z', 'claude-opus-5-5', inp=100, out=50, cr=1000),
            json.dumps({'type': 'user', 'message': {'content': 'hello'}}),
        ])
        self.write('proj-a/sess/subagents/agent-1.jsonl',
                   [asst('msg_B', '2026-10-25T10:31:00Z', 'claude-sonnet-5', out=7, cw1=300)])
        ledger.ingest_transcripts(self.db, self.projects)
        rows = self.db.execute('SELECT key, input, output, cache_read, cache_write_1h, is_subagent, project '
                               'FROM messages ORDER BY key').fetchall()
        self.assertEqual(rows, [('msg_A', 100, 50, 1000, 0, 0, 'proj-a'), ('msg_B', 0, 7, 0, 300, 1, 'proj-a')])

    def test_incremental_ingest_does_not_duplicate_and_waits_for_partial_lines(self):
        self.write('proj-a/sess.jsonl', [asst('msg_A', '2026-10-25T10:30:00Z', 'm', out=5)])
        ledger.ingest_transcripts(self.db, self.projects)
        # a line without a trailing newline is half-written: not consumed yet
        self.write('proj-a/sess.jsonl', [asst('msg_C', '2026-10-25T10:40:00Z', 'm', out=9)], 'a', newline=False)
        r = ledger.ingest_transcripts(self.db, self.projects)
        self.assertEqual(r['upserted_rows'], 0)
        self.write('proj-a/sess.jsonl', [''], 'a')  # the line is completed
        ledger.ingest_transcripts(self.db, self.projects)
        ledger.ingest_transcripts(self.db, self.projects)
        self.assertEqual(self.db.execute('SELECT count(*), sum(output) FROM messages').fetchone(), (2, 14))


class Capacity(Base):
    R1 = '2026-10-25T15:00:00Z'   # 5 h window: 10:00 → 15:00 UTC
    R2 = '2026-10-25T20:00:00Z'   # next 5 h window: 15:00 → 20:00 UTC
    R7 = '2026-10-30T08:00:00Z'   # weekly: Oct 23 08:00 → Oct 30 08:00

    def setUp(self):
        super().setUp()
        self.write('proj-a/sess.jsonl', [
            asst('m1', '2026-10-25T10:30:00Z', 'claude-opus-5-5', inp=1000),      # 1000 weighted
            asst('m2', '2026-10-25T11:30:00Z', 'claude-fable-5-1', out=200),      # 1000 weighted
            asst('m0', '2026-10-25T09:00:00Z', 'claude-opus-5-5', inp=99999),     # before the window
        ])
        ledger.ingest_transcripts(self.db, self.projects)
        with open(os.path.join(self.base, 'samples', 's1.jsonl'), 'w') as fh:
            for ts, pct in (('10:15', 5), ('10:45', 10), ('11:45', 20)):
                fh.write(sample(f'2026-10-25T{ts}:00Z', 'five_hour', pct, self.R1) + '\n')
                fh.write(sample(f'2026-10-25T{ts}:00Z', 'seven_day', pct, self.R7) + '\n')
                fh.write(sample(f'2026-10-25T{ts}:00Z', 'seven_day_overage_included', pct, self.R7) + '\n')
            # second 5 h window: rises 2 points without any local token → residual
            fh.write(sample('2026-10-25T15:30:00Z', 'five_hour', 3, self.R2) + '\n')
            fh.write(sample('2026-10-25T16:30:00Z', 'five_hour', 5, '2026-10-25T20:00:00.400Z') + '\n')
        self.assertEqual(ledger.import_samples(self.db, self.base), 11)
        self.assertEqual(ledger.import_samples(self.db, self.base), 0)  # incremental

    def rows(self, kind):
        return ledger.build_report(self.db, self.w, kind)

    def test_total_and_per_segment_capacity(self):
        r1, r2 = self.rows('five_hour')
        self.assertEqual((r1['pct'], r1['weighted']), (20, 2000))
        self.assertAlmostEqual(r1['capacity'], 10000)
        # segments: (10:15,10:45] 1000/5 % = 20000 and (10:45,11:45] 1000/10 % = 10000
        self.assertEqual([round(s['capacity']) for s in r1['segments']], [20000, 10000])
        self.assertAlmostEqual(r1['median'], 15000)
        self.assertEqual(r1['range'], (10000, 20000))
        self.assertEqual(r1['residual'], 0)
        # the reset with milliseconds is normalized to the minute: same window
        self.assertEqual((r2['n_samples'], r2['pct'], r2['residual'], r2['capacity']), (2, 5, 1, None))
        self.assertTrue(r2['segments'][0]['residual'])

    def test_fable_window_counts_only_fable_and_weekly_counts_all(self):
        (fable,) = self.rows('fable')
        (week,) = self.rows('seven_day')
        self.assertEqual(list(fable['by_model']), ['claude-fable-5-1'])
        self.assertAlmostEqual(fable['capacity'], 5000)
        # the weekly window also includes m0 (09:00, within the 7 days)
        self.assertAlmostEqual(week['weighted'], 2000 + 99999)

    def test_under_5_points_does_not_estimate(self):
        with open(os.path.join(self.base, 'samples', 's2.jsonl'), 'w') as fh:
            fh.write(sample('2026-10-25T11:50:00Z', 'seven_day_opus', 4.9, self.R7) + '\n')
        ledger.import_samples(self.db, self.base)
        (r,) = self.rows('seven_day_opus')
        self.assertIsNone(r['capacity'])
        self.assertIn('< 5 pts', ledger.render_text([r], self.db, self.w))

    def test_text_and_markdown_report(self):
        rows = self.rows(None)
        txt = ledger.render_text(rows, self.db, self.w)
        self.assertIn('residual (chats or other devices)', txt)
        md = ledger.render_md(rows, self.db, self.w)
        self.assertTrue(md.startswith('# Usage limits report'))

    def test_without_samples_says_so_instead_of_failing(self):
        self.db.execute('DELETE FROM samples')
        txt = ledger.render_text(ledger.build_report(self.db, self.w), self.db, self.w)
        self.assertIn('No percentage samples yet', txt)


if __name__ == '__main__':
    unittest.main()
