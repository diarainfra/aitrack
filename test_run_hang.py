"""Tunnijooks ei tohi igaveseks kinni jääda: protsessipuu tapmine, surnud luku ülevõtmine, valvur."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aitrack as A

# Laps käivitab lapselapse, mis hoiab stdout-toru lahti ja magab kaua (nagu claude CLI node-protsessid).
HANGING = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
    "time.sleep(120)"
)


class EngineTimeoutTests(unittest.TestCase):
    def test_timeout_kills_whole_tree_and_returns_quickly(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            A._run_engine([sys.executable, "-c", HANGING], "sisend", timeout=2)
        self.assertLess(time.monotonic() - start, 30)

    def test_normal_output_is_utf8(self):
        res = A._run_engine([sys.executable, "-c",
                             "import sys; d=sys.stdin.buffer.read().decode(); sys.stdout.buffer.write(('ok ' + d).encode())"],
                            "õun ✅", timeout=30)
        self.assertEqual((res.returncode, res.stdout), (0, "ok õun ✅"))


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "aitrack.lock"
        for name, value in (("LOCK_FILE", self.lock), ("CONFIG_DIR", Path(self.tmp.name))):
            p = patch.object(A, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(A, "log")
        p.start()
        self.addCleanup(p.stop)

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_pid_alive(self):
        self.assertTrue(A._pid_alive(os.getpid()))
        self.assertFalse(A._pid_alive(self.dead_pid()))

    def test_lock_of_dead_process_is_taken_over_immediately(self):
        self.lock.write_text(str(self.dead_pid()))
        self.assertEqual(A.acquire_lock(), self.lock)
        self.assertEqual(self.lock.read_text(), str(os.getpid()))

    def test_lock_of_live_process_is_respected(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(holder.kill)
        self.lock.write_text(str(holder.pid))
        self.assertIsNone(A.acquire_lock())


class WatchdogTests(unittest.TestCase):
    def test_stalled_run_dumps_stack_releases_lock_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "aitrack.log"
            lock = Path(tmp) / "aitrack.lock"
            lock.write_text("1")
            exited = threading.Event()
            with patch.object(A, "LOG_FILE", log_file), patch.object(A, "log"), \
                 patch.object(A.os, "_exit", side_effect=lambda code: exited.set()):
                stop = A._start_run_watchdog(lock, stall_seconds=1)
                self.assertTrue(exited.wait(10))
                stop.set()
            self.assertFalse(lock.exists())
            self.assertIn("File", log_file.read_text(encoding="utf-8"))   # virnajälg on logis

    def test_progress_keeps_watchdog_quiet(self):
        exited = threading.Event()
        with patch.object(A.os, "_exit", side_effect=lambda code: exited.set()), patch.object(A, "log"):
            stop = A._start_run_watchdog(None, stall_seconds=2)
            for _ in range(8):
                A._mark_progress()
                time.sleep(0.5)
            stop.set()
        self.assertFalse(exited.is_set())


if __name__ == "__main__":
    unittest.main()
