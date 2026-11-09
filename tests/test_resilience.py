"""The app's log, its health panel and the Web tab's browser link survive what real use throws at them: test runs, a log
that grows for weeks, the PC going to sleep, and the DevTools link dropping while Brave keeps running."""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health  # noqa: E402
import roku_listen_gui as g  # noqa: E402
import web_guard  # noqa: E402
import web_tab  # noqa: E402


class LogPlace(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        saved = (g.LOG, g.DEFAULT_LOG, g.TEST_LOG, g.LOG_MAX, dict(g._log_days), g._log_writes[0])

        def restore():
            g.LOG, g.DEFAULT_LOG, g.TEST_LOG, g.LOG_MAX = saved[:4]
            g._log_days.clear()
            g._log_days.update(saved[4])
            g._log_writes[0] = saved[5]
        self.addCleanup(restore)
        self.real = os.path.join(self.dir.name, "RokuListen.log")        # stands in for %APPDATA%\RokuListen.log
        self.test = os.path.join(self.dir.name, "RokuListen.test.log")
        g.DEFAULT_LOG = g.LOG = self.real
        g.TEST_LOG = self.test

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_test_run_never_writes_into_the_real_log(self):
        g.log("sync load v1 t=330")                                       # what a fake video looks like
        self.assertFalse(os.path.exists(self.real))
        self.assertIn("sync load v1", self.read(self.test))

    def test_a_path_set_on_purpose_wins(self):
        chosen = os.path.join(self.dir.name, "chosen.log")
        g.LOG = chosen
        g.log("hello")
        self.assertIn("hello", self.read(chosen))
        self.assertFalse(os.path.exists(self.test))

    def test_the_built_app_always_uses_its_log_even_if_unittest_happens_to_be_loaded(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            g.log("from the exe")
        self.assertIn("from the exe", self.read(self.real))

    def test_each_new_day_is_marked_once(self):
        g.log("one")
        g.log("two")
        text = self.read(self.test)
        self.assertEqual(text.count("====="), 2)                          # one marker line: "===== <day> ====="
        self.assertIn(time.strftime("%Y-%m-%d"), text.splitlines()[0])
        g._log_days[self.test] = "2000-01-01"                             # as if midnight had passed
        g.log("three")
        self.assertEqual(self.read(self.test).count(time.strftime("%Y-%m-%d")), 2)

    def test_a_big_log_is_moved_aside_and_a_fresh_one_starts(self):
        g.LOG_MAX = 50
        for i in range(5):
            g.log(f"line {i} " + "x" * 20)
        g._log_writes[0] = 200                                            # the next write is a size-check write
        g.log("after the cut")
        self.assertTrue(os.path.exists(self.test + ".1"))
        self.assertIn("line 0", self.read(self.test + ".1"))
        fresh = self.read(self.test)
        self.assertNotIn("line 0", fresh)
        self.assertTrue(fresh.startswith("====="))                        # the fresh file opens with the date
        self.assertIn("after the cut", fresh)

    def test_the_ffmpeg_side_log_follows_the_same_rule(self):
        self.assertEqual(g.log_path(), self.test)                         # so a test can't truncate the live one


class SleepIsNotAFreeze(unittest.TestCase):
    def test_lateness_minus_sleep(self):
        self.assertEqual(health.split_late(10.0, None), 10.0)            # sleep unknown: all of it counts
        self.assertAlmostEqual(health.split_late(7258.7, 7258.5), 0.2)
        self.assertEqual(health.split_late(10.0, 20.0), 0.0)

    def test_a_night_asleep_is_noted_as_sleep_not_as_an_app_stall(self):
        h = health.Health()
        h.pc_slept(7258.5)
        s = h.snapshot()
        self.assertEqual(s["asleep"], 1)
        self.assertEqual(s["stalls"]["app"], 0)
        self.assertIn("PC asleep 1x, not counted", h.report())

    def test_a_blink_is_not_reported(self):
        h = health.Health()
        h.pc_slept(2)
        self.assertEqual(h.snapshot()["asleep"], 0)

    def test_the_sleep_clock_reads_on_windows(self):
        a = health.suspended_seconds()
        if a is None:
            self.skipTest("not Windows")
        b = health.suspended_seconds()
        self.assertGreaterEqual(a, 0.0)
        self.assertGreaterEqual(b, a - 0.05)                              # never runs backwards (beyond clock jitter)


class FakeGuard(web_guard.WebGuard):
    """A guard that 'connects' without a browser."""
    fail = False
    attached = []

    def attach(self, port_file):
        if FakeGuard.fail:
            raise ConnectionRefusedError("browser busy")
        FakeGuard.attached.append(port_file)
        self.running, self.error, self.attached_at = True, "", time.time()


class Reconnect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        FakeGuard.fail, FakeGuard.attached = False, []
        patcher = mock.patch.object(web_tab, "WebGuard", FakeGuard)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.ui = SimpleNamespace(log=self.logs.append)
        tab.profile, tab.home = self.dir.name, "https://home.example/"
        old = web_guard.WebGuard(lambda m: None, home=tab.home)
        old.error, old._view = web_guard.LOST, (900, 600, True)
        old._url, old._page_key = "https://site.example/watch/1", "https://site.example/watch/1"
        old._add_candidate("https://cdn.example/ep1.m3u8", "hls", "f1", {"Referer": "r"}, 0)
        tab.guard, self.old = old, old
        tab.sync = SimpleNamespace(guard=old, epoch0=7, active=True)
        self.tab = tab
        with open(os.path.join(self.dir.name, "DevToolsActivePort"), "w") as f:
            f.write("9222\n/devtools/browser/x")

    def test_a_dropped_link_is_rebuilt_and_everything_moves_to_the_new_guard(self):
        self.tab._reattach()
        new = self.tab.guard
        self.assertIsNot(new, self.old)
        self.assertTrue(new.running)
        self.assertEqual(new._view, (900, 600, True))                     # same size, still streaming
        self.assertEqual([c.url for c in new.candidates()], ["https://cdn.example/ep1.m3u8"])   # Cast still works
        self.assertEqual(new.snapshot()["url"], "https://site.example/watch/1")
        self.assertIs(self.tab.sync.guard, new)                           # the cast in progress follows the new guard
        self.assertEqual(self.tab.sync.epoch0, new.epoch)                 # ...and doesn't think you left the page
        self.assertTrue(any("reconnected" in m for m in self.logs))

    def test_no_browser_no_attempt(self):
        os.remove(os.path.join(self.dir.name, "DevToolsActivePort"))
        self.tab._reattach()
        self.assertIs(self.tab.guard, self.old)
        self.assertEqual(FakeGuard.attached, [])

    def test_a_browser_that_keeps_refusing_is_retried_less_and_less_often(self):
        FakeGuard.fail = True
        waits = []
        for _ in range(8):
            before = time.time()
            self.tab._reattach()
            waits.append(round(self.tab._reattach_at - before))
        self.assertIs(self.tab.guard, self.old)
        self.assertEqual(waits[:4], [4, 8, 16, 32])
        self.assertTrue(all(w == 60 for w in waits[4:]))                  # capped at a minute
        self.assertTrue(any("reconnect failed" in m for m in self.logs))

    def test_waking_up_checks_at_once(self):
        self.tab._reattach_at = time.time() + 50
        self.tab.on_resume()
        self.assertEqual(self.tab._reattach_at, 0.0)


if __name__ == "__main__":
    unittest.main()
