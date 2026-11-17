"""Codex token usage regressions with synthetic scoped logs only."""
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aitrack as A


def row(kind, payload):
    return json.dumps(dict(type=kind, timestamp='2026-11-17T10:00:00Z', payload=payload)) + '\n'


UTC = dt.timezone.utc
SINCE = dt.datetime(2026, 11, 17, 10, tzinfo=UTC)


def usage(i=100, cached=40, output=20, reasoning=5, timestamp='2026-11-17T10:15:00Z'):
    item = json.loads(row('event_msg', dict(type='token_count', info=dict(total_token_usage=dict(
        input_tokens=i, cached_input_tokens=cached, output_tokens=output,
        reasoning_output_tokens=reasoning)))))
    item['timestamp'] = timestamp
    return json.dumps(item) + '\n'


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        for name, value in [('CODEX_SESSIONS', self.sessions)]:
            p = patch.object(A, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(A, 'load_projects', return_value=[str(self.project)])
        p.start()
        self.addCleanup(p.stop)

    def log(self, body, cwd=None, model='test-model', effort='high'):
        path = self.sessions / 'rollout-test.jsonl'
        path.write_text(row('session_meta', dict(id='test', cwd=str(cwd or self.project))) +
                        row('turn_context', dict(cwd=str(cwd or self.project), model=model, effort=effort)) + body)

    def test_deltas_duplicates_cache_and_reasoning(self):
        self.log(usage() + usage() + usage(i=250, cached=100, output=50, reasoning=15))
        records = A.codex_token_usage(SINCE)
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(r.input for r in records), 150)
        self.assertEqual(sum(r.cache_read for r in records), 100)
        self.assertEqual(sum(r.output for r in records), 50)
        self.assertEqual(sum(r.reasoning for r in records), 15)
        self.assertEqual(sum(r.input + r.cache_read + r.output for r in records), 300)
        self.assertEqual({r.model for r in records}, {'test-model'})
        self.assertEqual({r.thinking_level for r in records}, {'high'})

    def test_window_baseline_includes_earlier_events(self):
        self.log(usage(timestamp='2026-11-17T09:59:00Z') +
                 usage(i=150, cached=60, output=30, reasoning=8))
        records = A.codex_token_usage(SINCE)
        self.assertEqual([(r.input, r.cache_read, r.output, r.reasoning) for r in records], [(30, 20, 10, 3)])

    def test_disallowed_session_and_scope_change(self):
        self.log(usage(), cwd=self.root / 'other')
        self.assertEqual(A.codex_token_usage(SINCE), [])
        self.log(usage() + row('turn_context', dict(cwd=str(self.root / 'other'), model='private')) +
                 usage(i=200))
        self.assertEqual(len(A.codex_token_usage(SINCE)), 1)
        with patch.object(A, 'load_projects', return_value=[]), patch.object(Path, 'open', side_effect=AssertionError):
            self.assertEqual(A.codex_token_usage(SINCE), [])

    def test_missing_model_inconsistent_counts_and_counter_reset(self):
        self.log(usage(), model='')
        self.assertEqual(A.codex_token_usage(SINCE), [])
        self.log(usage(i=10, cached=20))
        self.assertEqual(A.codex_token_usage(SINCE), [])
        self.log(usage() + usage(i=10, cached=4, output=2, reasoning=0) +
                 usage(i=30, cached=10, output=7, reasoning=1))
        self.assertEqual([(r.input, r.output) for r in A.codex_token_usage(SINCE)], [(60, 20), (14, 5)])

    def test_model_change_and_server_rows(self):
        self.log(usage() + row('turn_context', dict(cwd=str(self.project), model='next-model', effort='low')) +
                 usage(i=150, cached=60, output=30, reasoning=8))
        with patch.object(A, 'claude_token_usage', return_value=[]), patch.object(A, 'pi_token_usage', return_value=[]):
            records = A.collect_token_usage(SINCE)
        rows = A._token_usage_rows(records, [str(self.project)], group_by='hour',
                                   start=SINCE, end=SINCE + dt.timedelta(hours=1), tz=UTC)
        self.assertEqual({r['model'] for r in rows}, {'test-model', 'next-model'})
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'server.db'
            token = A._db_add_user(db, 'test')
            for _ in range(2):
                self.assertEqual(A._db_ingest_token_usage(db, token, rows)['stored'], 2)
            with A._db_connect(db) as conn:
                count, inputs, outputs = conn.execute(
                    'SELECT COUNT(*), SUM(input_tokens + cache_read_tokens), SUM(output_tokens) FROM token_usage'
                ).fetchone()
            self.assertEqual((count, inputs, outputs), (2, 150, 30))

    def test_cache_writes_are_not_counted_twice(self):
        item = json.loads(usage())
        item['payload']['info']['total_token_usage']['cache_write_input_tokens'] = 10
        self.log(json.dumps(item) + '\n')
        record, = A.codex_token_usage(SINCE)
        self.assertEqual((record.input, record.cache_read, record.cache_write, record.output), (50, 40, 10, 20))

    def test_rejected_session_body_is_not_consumed(self):
        from aitrack_core.codex import _scoped_objects
        class Capture(io.StringIO):
            def close(self):
                self.position_at_close = self.tell()
                super().close()
        first = row('session_meta', dict(cwd=str(self.root / 'private')))
        stream = Capture(first + usage())
        with patch.object(Path, 'open', return_value=stream):
            self.assertEqual(list(_scoped_objects(self.root / 'unknown.jsonl', lambda cwd: False)), [])
        self.assertEqual(stream.position_at_close, len(first))

    def test_unknown_metadata_and_malformed_usage(self):
        path = self.sessions / 'rollout-test.jsonl'
        for first in ['null\n', '{}\n', row('session_meta', dict(cwd='')), row('session_meta', dict(cwd='relative'))]:
            path.write_text(first + usage())
            self.assertEqual(A.codex_token_usage(SINCE), [])
        self.log(row('event_msg', dict(type='token_count', info=None)) +
                 row('event_msg', dict(type='token_count', info=dict(total_token_usage=dict(input_tokens='bad')))) +
                 usage())
        self.assertEqual(len(A.codex_token_usage(SINCE)), 1)

    def test_reasoning_level_fallback_and_guardian_exclusion(self):
        self.log(usage(), effort='')
        record, = A.codex_token_usage(SINCE)
        self.assertEqual(record.thinking_level, 'on')
        path = self.sessions / 'rollout-test.jsonl'
        path.write_text(row('session_meta', dict(cwd=str(self.project), source=dict(subagent='guardian'))) +
                        row('turn_context', dict(cwd=str(self.project), model='test')) + usage())
        self.assertEqual(A.codex_token_usage(SINCE), [])
