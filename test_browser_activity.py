"""Peidetud aktiivsete minutite arvestus: AI-logid + brauser (ajalugu ja laiendus), kattuvused üks kord."""
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aitrack as A
from aitrack_core import browser as B

UTC = dt.timezone.utc


def t(hh, mm, ss=0):
    return dt.datetime(2026, 11, 17, hh, mm, ss, tzinfo=UTC)


class UrlMatchingTests(unittest.TestCase):
    def test_normalize_drops_scheme_www_query_and_case(self):
        self.assertEqual(B.normalize_url("https://www.GitHub.com/Org/Repo/?tab=x#y"), "github.com/org/repo")
        self.assertEqual(B.normalize_url("http://localhost:3103/activity"), "localhost:3103/activity")

    def test_strip_url_removes_query_fragment_and_credentials(self):
        self.assertEqual(B.strip_url("https://u:p@site.ee/a?token=1#x"), "https://site.ee/a")
        self.assertEqual(B.strip_url("chrome://settings"), "")

    def test_most_specific_pattern_wins_and_unrelated_is_none(self):
        pats = {"/p/a": ["github.com/org"], "/p/b": ["github.com/org/b"]}
        self.assertEqual(B.assign_project("https://github.com/org/b/pull/3", pats), "/p/b")
        self.assertEqual(B.assign_project("https://github.com/org/a", pats), "/p/a")
        self.assertIsNone(B.assign_project("https://github.com/organisation", pats))
        self.assertIsNone(B.assign_project("https://youtube.com/watch", pats))

    def test_host_pattern_matches_subdomain_but_not_lookalike(self):
        pats = {"/p": ["python.org"]}
        self.assertEqual(B.assign_project("https://docs.python.org/3/", pats), "/p")
        self.assertIsNone(B.assign_project("https://notpython.org/", pats))


class MinuteMathTests(unittest.TestCase):
    def test_visit_ends_at_next_visit_even_if_unrelated(self):
        visits = {"x": [B.BrowserVisit("Chrome", t(10, 0), "https://github.com/o/r"),
                        B.BrowserVisit("Chrome", t(10, 2), "https://youtube.com/")]}
        spans = B.visits_to_spans(visits, max_minutes=5)
        self.assertEqual(spans[0].end, t(10, 2))
        self.assertEqual(spans[1].end, t(10, 7))   # viimane: lagi 5 min

    def test_chromium_duration_shortens_span(self):
        spans = B.visits_to_spans({"x": [B.BrowserVisit("Edge", t(10, 0), "u", duration_s=60)]}, max_minutes=5)
        self.assertEqual(spans[0].end, t(10, 1))

    def test_ai_minutes_fill_short_gaps_only(self):
        mins = B.ai_minutes([t(10, 0), t(10, 5), t(10, 40)], gap_minutes=10, tail_minutes=2)
        # 10:00–10:07 (täidetud vahe + saba) ja 10:40–10:42
        self.assertEqual(len(mins), 7 + 2)

    def test_periods_merge_contiguous_minutes(self):
        mins = B.span_minutes(t(10, 0), t(10, 3)) | B.span_minutes(t(10, 2), t(10, 5)) | B.span_minutes(t(10, 9), t(10, 10))
        self.assertEqual(B.minutes_to_periods(mins), [(t(10, 0), t(10, 5)), (t(10, 9), t(10, 10))])


class ActiveRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = str(Path(self.tmp.name).resolve() / "proj")
        Path(self.proj).mkdir()
        self.cfg = A.load_config()
        self.cfg["browser"] = dict(self.cfg["browser"], urls={self.proj: ["github.com/org/proj"]})
        A._PROJECT_KEY_CACHE.clear()

    def rows(self, records, spans):
        return A._active_period_rows(records, [], [self.proj], self.cfg, start=t(10, 0), end=t(12, 0),
                                     tz=UTC, browser_spans=spans)

    def test_five_minutes_of_work_is_not_a_full_hour(self):
        rows = self.rows([A.Record("Claude", self.proj, t(10, 12), "x"), A.Record("Claude", self.proj, t(10, 15), "y")], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["minutes"], 5)        # 10:12–10:17
        self.assertEqual(rows[0]["browser_minutes"], 0)

    def test_browser_and_ai_overlap_is_counted_once_and_unrelated_ignored(self):
        records = [A.Record("Claude", self.proj, t(10, 12), "x"), A.Record("Claude", self.proj, t(10, 15), "y")]
        spans = [
            B.BrowserSpan("Chrome", "https://github.com/org/proj/issues/1", t(10, 14), t(10, 20), "history"),
            B.BrowserSpan("Edge", "https://github.com/org/proj", t(10, 16), t(10, 18), "extension"),
            B.BrowserSpan("Chrome", "https://youtube.com/", t(10, 30), t(10, 50), "history"),
        ]
        row = self.rows(records, spans)[0]
        self.assertEqual(row["ai_minutes"], 5)          # 12..16
        self.assertEqual(row["browser_minutes"], 6)     # 14..19
        self.assertEqual(row["overlap_minutes"], 3)     # 14,15,16
        self.assertEqual(row["minutes"], 8)             # 12..19, mitte 5+6+2
        self.assertEqual(row["periods"], [{"start": t(10, 12).isoformat(), "end": t(10, 20).isoformat()}])
        self.assertEqual(row["browsers"], ["Chrome", "Edge"])

    def test_browser_only_hour_and_hour_split(self):
        spans = [B.BrowserSpan("Chrome", "https://github.com/org/proj", t(10, 58), t(11, 3), "history")]
        rows = self.rows([], spans)
        self.assertEqual([(r["hour_utc"], r["minutes"]) for r in rows],
                         [(t(10, 0).isoformat(), 2), (t(11, 0).isoformat(), 3)])
        self.assertTrue(all(r["minutes"] <= 60 for r in rows))

    def test_minutes_outside_processing_window_are_dropped(self):
        spans = [B.BrowserSpan("Chrome", "https://github.com/org/proj", t(11, 58), t(12, 10), "history")]
        self.assertEqual(sum(r["minutes"] for r in self.rows([], spans)), 2)


class HistoryAndExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reads_chromium_history_copy(self):
        db = self.root / "History"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
        conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, visit_duration INTEGER)")
        conn.execute("INSERT INTO urls VALUES (1, 'https://github.com/org/proj')")
        to_chrome = lambda d: int(d.timestamp() * 1e6) + B.CHROME_EPOCH_OFFSET_US  # noqa: E731
        conn.execute("INSERT INTO visits VALUES (1, 1, ?, 0)", (to_chrome(t(10, 5)),))
        conn.execute("INSERT INTO visits VALUES (2, 1, ?, 0)", (to_chrome(t(9, 5)),))
        conn.commit()
        conn.close()
        got = B.read_history_visits([("Chrome", "chromium", db)], t(10, 0), t(11, 0))
        visits = next(iter(got.values()))
        self.assertEqual([(v.ts, v.url) for v in visits], [(t(10, 5), "https://github.com/org/proj")])

    def test_finds_profiles(self):
        prof = self.root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Profile 2"
        prof.mkdir(parents=True)
        (prof / "History").write_bytes(b"")
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "AppData" / "Local"),
                                       "APPDATA": str(self.root / "AppData" / "Roaming")}):
            found = B.history_sources(self.root, "windows")
        self.assertEqual([(n, k) for n, k, _ in found], [("Chrome", "chromium")])

    def test_extension_samples_roundtrip_and_prune(self):
        log = self.root / "browser_activity.jsonl"
        n = B.append_extension_samples(log, [
            {"ts": "2026-11-17T10:05:00.000Z", "url": "https://github.com/org/proj?secret=1", "browser": "Chrome"},
            {"ts": "2026-11-01T10:05:00Z", "url": "https://old.ee/", "browser": "Chrome"},
            {"ts": "2026-11-17T10:06:00Z", "url": "file:///etc/passwd"},
        ])
        self.assertEqual(n, 2)
        self.assertNotIn("secret", log.read_text(encoding="utf-8"))
        spans = B.read_extension_spans(log, t(10, 0), t(11, 0))
        self.assertEqual([(s.start, s.end) for s in spans], [(t(10, 5), t(10, 6))])
        B.prune_extension_log(log, 14, t(12, 0) + dt.timedelta(days=1))
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

    def test_native_messaging_protocol(self):
        def frame(obj):
            data = json.dumps(obj).encode()
            return len(data).to_bytes(4, "little") + data
        stdin = io.BytesIO(frame({"type": "ping"}) + frame({"type": "samples", "samples": [1]}))
        stdout = io.BytesIO()
        with patch.object(B.sys, "byteorder", "little"):
            B.native_host_loop(stdin, stdout, lambda m: {"ok": True, "type": m.get("type")})
        out = stdout.getvalue()
        first_len = int.from_bytes(out[:4], "little")
        self.assertEqual(json.loads(out[4:4 + first_len]), {"ok": True, "type": "ping"})


class BrowserSetupTests(unittest.TestCase):
    def run_setup(self, browsers, **kw):
        out = io.StringIO()
        opened = []
        with patch.object(A, "_install_browser_native_host", return_value=["ok"]) as host, \
             patch.object(A, "_find_extension_browsers", return_value=browsers), \
             patch.object(A, "_copy_to_clipboard", return_value=True) as clip, \
             patch.object(A.subprocess, "Popen", side_effect=lambda cmd, **_: opened.append(cmd)), \
             patch("sys.stdout", out):
            A.cmd_browser_setup(A.argparse.Namespace(browser=kw.get("browser"), no_open=kw.get("no_open", False)), {})
        return out.getvalue(), host, clip, opened

    def test_setup_registers_copies_path_and_opens_extensions_page(self):
        text, host, clip, opened = self.run_setup([("Chrome", "chrome://extensions", ["chrome.exe"]),
                                                   ("Edge", "edge://extensions", ["msedge.exe"])])
        host.assert_called_once()
        clip.assert_called_once_with(str(A.BROWSER_EXTENSION_DIR))
        self.assertEqual(opened, [["chrome.exe", "chrome://extensions"]])
        self.assertIn("Load unpacked", text)
        self.assertIn(str(A.BROWSER_EXTENSION_DIR), text)
        self.assertIn("Edge", text)        # vihje teise brauseri kohta

    def test_browser_choice_and_no_open(self):
        _, _, _, opened = self.run_setup([("Chrome", "chrome://extensions", ["c"]), ("Edge", "edge://extensions", ["e"])],
                                         browser="edge", no_open=True)
        self.assertEqual(opened, [])

    def test_no_chromium_browser_explains_history_fallback(self):
        text, host, _, opened = self.run_setup([])
        host.assert_called_once()
        self.assertEqual(opened, [])
        self.assertIn("ajaloost", text)

    def test_extension_files_are_complete(self):
        manifest = json.loads((A.BROWSER_EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["background"], {"service_worker": "background.js"})
        for rel in ["background.js", *manifest["icons"].values()]:
            self.assertTrue((A.BROWSER_EXTENSION_DIR / rel).is_file(), rel)
        self.assertIn("nativeMessaging", manifest["permissions"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        # Windows: serveri SQLite ühendus võib faili hetkeks lukus hoida
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "server.db"
        self.tok = A._db_add_user(self.db, "karl")
        A._db_allow_projects(self.db, self.tok, {"projects": [{"root_path": "/w/proj", "local_path": "/w/proj",
                                                               "project_key": "github.com/org/proj", "name": "proj"}]})

    def row(self, **kw):
        base = {"hour_utc": t(10, 0).isoformat(), "date": "2026-11-17", "hour": "12:00–13:00",
                "project_key": "github.com/org/proj", "project": "proj", "local_path": "/w/proj",
                "minutes": 999, "ai_minutes": 5, "browser_minutes": 6, "overlap_minutes": 3, "browsers": ["Chrome"],
                "periods": [{"start": t(9, 50).isoformat(), "end": t(10, 8).isoformat()},
                            {"start": t(10, 5).isoformat(), "end": t(10, 10).isoformat()}]}
        base.update(kw)
        return base

    def test_ingest_clips_merges_upserts_and_respects_allowlist(self):
        res = A._db_ingest_active_periods(self.db, self.tok, [self.row(), self.row(project_key="github.com/x/other",
                                                                                  local_path="/w/other")])
        self.assertEqual((res["stored"], res["skipped"]), (1, 1))
        A._db_ingest_active_periods(self.db, self.tok, [self.row()])   # kordus → upsert, mitte duplikaat
        out = A._db_export_active_periods(self.db, self.tok, {"from": "2026-11-17", "to": "2026-11-17"})
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["minutes"], 10)    # 10:00–10:10, kliendi 999 ei loe
        self.assertEqual(out["rows"][0]["periods"], [{"start": t(10, 0).isoformat(), "end": t(10, 10).isoformat()}])


if __name__ == "__main__":
    unittest.main()
