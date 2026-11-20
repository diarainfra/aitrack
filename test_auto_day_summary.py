"""Automaatne päevakokkuvõte pärast südaööd: ainult puuduvatele päevadele, olemasolevat ei kirjutata üle."""
import datetime as dt
import unittest
from unittest.mock import patch

import aitrack as A

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 20, 0, 5, tzinfo=UTC)   # kohe pärast südaööd (UTC tsoonis)


class AutoDaySummaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = A.load_config()
        self.cfg["timezone"] = "UTC"
        self.cfg["sink"] = {"type": "server", "url": "https://x", "token": "t"}
        self.summaries = {"2026-11-18": "olemas juba"}           # käsitsi tehtud
        self.rows = {"2026-11-18": [{"hour": "10"}], "2026-11-19": [{"hour": "11"}]}
        self.posts = []
        self.saved_states = []

    def server_get(self, op, params, cfg):
        date = params["date"]
        if op == "day-summary":
            return {"ok": True, "summary": {"summary": self.summaries.get(date, "")}}
        if op == "day":
            return {"ok": True, "rows": self.rows.get(date, [])}
        raise AssertionError(op)

    def server_post(self, op, payload, cfg):
        self.posts.append((op, payload))
        self.summaries[payload["date"]] = payload["summary"]
        return {"ok": True}

    def run_auto(self, state, engine=("tekst", "claude", "m"), resolve=("claude", "x")):
        with patch.object(A, "_now_utc", return_value=NOW), \
             patch.object(A, "_server_get", side_effect=self.server_get), \
             patch.object(A, "_server_post", side_effect=self.server_post), \
             patch.object(A, "_generate_day_summary", return_value=engine) as gen, \
             patch.object(A, "resolve_engine", return_value=resolve), \
             patch.object(A, "save_state", side_effect=lambda s: self.saved_states.append(dict(s))), \
             patch.object(A, "log"):
            A._auto_day_summaries(self.cfg, state)
        return gen

    def test_creates_only_missing_days_with_rows_and_keeps_existing(self):
        state = {}
        gen = self.run_auto(state)
        self.assertEqual([p[1]["date"] for p in self.posts], ["2026-11-19"])
        self.assertEqual(self.summaries["2026-11-18"], "olemas juba")
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(state["day_summary_checked_until"], "2026-11-19")
        self.assertIn("aitrack auto", self.posts[0][1]["source"])

    def test_today_is_never_summarised(self):
        self.rows["2026-11-20"] = [{"hour": "00"}]
        self.run_auto({})
        self.assertNotIn("2026-11-20", [p[1]["date"] for p in self.posts])

    def test_checked_days_are_not_requested_again(self):
        state = {"day_summary_checked_until": "2026-11-19"}
        gen = self.run_auto(state)
        self.assertEqual(self.posts, [])
        gen.assert_not_called()

    def test_ai_failure_retries_later_without_saving(self):
        state = {}
        self.run_auto(state, engine=("üldine tekst", "fallback", ""))
        self.assertEqual(self.posts, [])
        self.assertEqual(state.get("day_summary_checked_until"), "2026-11-18")   # 19. jääb uuesti proovimiseks

    def test_server_down_does_not_advance(self):
        state = {}
        with patch.object(A, "_now_utc", return_value=NOW), \
             patch.object(A, "_server_get", return_value=None), \
             patch.object(A, "save_state"), patch.object(A, "log"):
            A._auto_day_summaries(self.cfg, state)
        self.assertNotIn("day_summary_checked_until", state)

    def test_disabled_or_not_server_sink(self):
        for cfg_change in ({"auto_day_summary": False}, {"sink": {"type": "local"}}):
            cfg = dict(self.cfg, **cfg_change)
            with patch.object(A, "_server_get") as get:
                A._auto_day_summaries(cfg, {})
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
