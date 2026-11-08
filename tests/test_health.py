"""The connection health panel: who gets blamed for a stall, what's counted, and how the pieces report in."""
import json
import os
import sys
import threading
import tkinter as tk
import unittest
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health  # noqa: E402
import roku_listen_gui as g  # noqa: E402
from yt_bridge import Bridge  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Classify(unittest.TestCase):
    def test_an_ordinary_gap_is_nobodys_fault(self):
        self.assertIsNone(health.classify(0.3))
        self.assertIsNone(health.classify(1.4, app_late=2.0))

    def test_the_app_freezing_is_the_app(self):
        self.assertEqual(health.classify(3.0, app_late=2.5, timer_gap=3.0), health.APP)

    def test_the_apps_own_clocks_fine_but_the_page_timer_late_is_the_browser(self):
        self.assertEqual(health.classify(4.0, app_late=0.0, timer_gap=3.9), health.CHROME)

    def test_both_fine_but_a_slow_round_trip_is_the_link(self):
        self.assertEqual(health.classify(2.0, app_late=0.1, timer_gap=0.3, rtt=1.8), health.LINK)

    def test_nothing_explains_it_says_so(self):
        self.assertEqual(health.classify(2.0, 0.0, 0.3, 0.1), health.UNKNOWN)


class Tracking(unittest.TestCase):
    def setUp(self):
        self.clock, self.lines = Clock(), []
        self.h = health.Health(self.clock, self.lines.append)

    def test_one_missed_look_at_the_tv_is_noise_two_is_down_and_an_answer_brings_it_back(self):
        self.h.tv_poll(True, 8)
        self.h.tv_poll(False)
        self.assertTrue(self.h.snapshot()["tv_ok"])
        self.h.tv_poll(False)
        self.assertFalse(self.h.snapshot()["tv_ok"])
        self.assertEqual(self.h.snapshot()["tv_down"], 1)
        self.h.tv_poll(False)
        self.assertEqual(self.h.snapshot()["tv_down"], 1)            # one event per outage, not one per second
        self.h.tv_poll(True, 12)
        self.assertEqual((self.h.snapshot()["tv_ok"], self.h.snapshot()["tv_ms"]), (True, 12))

    def test_buffering_is_counted_with_how_long_it_lasted(self):
        self.h.tv_playing("play", "Playlet")
        self.h.tv_playing("buffer", "Playlet")
        self.clock.t += 4.2
        self.h.tv_playing("play", "Playlet")
        s = self.h.snapshot()
        self.assertEqual(s["buffers"], 1)
        self.assertAlmostEqual(s["buffer_seconds"], 4.2, places=1)

    def test_a_blip_of_buffering_under_half_a_second_is_not_counted(self):
        self.h.tv_playing("buffer", "Playlet")
        self.clock.t += 0.3
        self.h.tv_playing("play", "Playlet")
        self.assertEqual(self.h.snapshot()["buffers"], 0)

    def test_reloads_and_avoided_reloads_are_counted_apart(self):
        self.h.reload(False, "load a")
        self.h.reload(True, "load a")
        self.h.reload(True, "load a")
        s = self.h.snapshot()
        self.assertEqual((s["reloads"], s["reloads_skipped"]), (1, 2))

    def test_history_is_forgotten_after_half_an_hour(self):
        self.h.reload(False, "old")
        self.clock.t += health.WINDOW + 1
        self.h.reload(False, "new")
        self.assertEqual(self.h.snapshot()["reloads"], 1)

    def test_a_page_poll_gap_is_blamed_on_the_browser_when_only_its_timer_was_late(self):
        self.h.page_poll()
        self.clock.t += 5.0
        self.assertEqual(self.h.page_poll(timer_gap=4.8, rtt=0.05), health.CHROME)
        self.assertEqual(self.h.snapshot()["stalls"]["browser"], 1)

    def test_a_gap_the_apps_own_watchdog_saw_is_blamed_on_the_app_and_counted_once(self):
        self.h.page_poll()
        self.clock.t += 3.0
        self.h.app_late(2.8)
        self.assertEqual(self.h.page_poll(timer_gap=3.0), health.APP)
        self.assertEqual(self.h.snapshot()["stalls"]["app"], 1)

    def test_normal_polling_and_a_closed_page_report_nothing(self):
        for _ in range(10):
            self.h.page_poll()
            self.clock.t += 0.25
        self.clock.t += 900                                       # the page was closed for a quarter of an hour
        self.h.page_poll()
        self.assertEqual(sum(self.h.snapshot()["stalls"].values()), 0)

    def test_the_report_is_readable_text_with_the_recent_events(self):
        self.h.tv_poll(True, 7)
        self.h.reload(False, "load abc at 5s")
        text = self.h.report(["Audio: fine"])
        self.assertIn("answering", text)
        self.assertIn("1 Playlet reloads", text)
        self.assertIn("Audio: fine", text)
        self.assertIn("load abc at 5s", text)

    def test_events_also_go_to_the_log(self):
        self.h.reload(False, "load abc")
        self.assertTrue(any("reload" in line and "load abc" in line for line in self.lines))


class BridgeReports(unittest.TestCase):
    def test_a_poll_tells_the_panel_what_the_page_measured(self):
        b, seen = Bridge(), []
        b.on_poll = lambda gap, rtt: seen.append((gap, rtt))
        b._note_poll({"gap": ["2.50"], "rtt": ["0.10"]})
        b._note_poll({})
        b._note_poll({"gap": ["junk"]})
        b._note_poll({"gap": ["-5"], "rtt": ["999999"]})
        self.assertEqual(seen, [(2.5, 0.1), (0.0, 0.0), (0.0, 3600.0)])

    def test_a_broken_callback_cannot_break_the_poll(self):
        b = Bridge()
        b.on_poll = lambda gap, rtt: 1 / 0
        b._note_poll({"gap": ["1"]})


class Extension(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "feed.js"),
                  encoding="utf-8") as f:
            cls.src = f.read()

    def test_the_page_reports_its_own_timer_gap_and_round_trip(self):
        self.assertIn("state.gap", self.src)
        self.assertIn("state.rtt", self.src)

    def test_a_late_tick_does_not_count_as_the_app_going_silent(self):
        self.assertIn("if (late > 1500 && lastReply) lastReply = tickNow;", self.src)

    def test_the_version_was_bumped_so_a_stale_copy_is_noticeable(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "manifest.json")) as f:
            self.assertEqual(json.load(f)["version"], "1.2.4")


class CastSyncCounts(unittest.TestCase):
    def test_a_reload_and_a_skipped_one_reach_the_panel(self):
        sync = g.CastSync.__new__(g.CastSync)
        sync.current, sync.title, sync.q, sync.on_now = "v1", "", None, None
        sync.settle_until, sync.lead, sync.last_check, sync._last_seek = 0.0, 2.5, 0.0, None
        sync.bridge, sync.host_fn, sync.health = SimpleNamespace(player=lambda: None), lambda: "tv", health.Health()
        playing = {"state": "play", "app": "Playlet", "pos": 330.0, "dur": 900}
        with mock.patch.object(g.Remote, "state", return_value=playing), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "v1"}), \
                mock.patch.object(g.Remote, "cast"), mock.patch.object(sync, "_started", return_value=False):
            sync._handle({"kind": "load", "id": "v1", "t": 325})              # already there: avoided
            sync._handle({"kind": "load", "id": "v1", "t": 10})               # somewhere else: a real reload
        s = sync.health.snapshot()
        self.assertEqual((s["reloads"], s["reloads_skipped"]), (1, 1))


class Window(unittest.TestCase):
    """The real panel on real widgets, in a Tk root that is never shown."""

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def make(self, listener=None):
        r = self.root
        r.health, r._host, r.listener = health.Health(), "127.0.0.1", listener
        r.bridge = SimpleNamespace(connected=lambda: True)
        r.audio_health = lambda: g.App.audio_health(r)
        r.health_report = lambda: g.App.health_report(r)
        with mock.patch.object(g.Remote, "_open", return_value=b"{}"):
            win = g.HealthWindow(r)
            win.withdraw()
            self.addCleanup(win.destroy)
            win.refresh()
            win.update_idletasks()
        return win

    def text(self, win, key):
        return win.rows[key][2].cget("text")

    def test_it_shows_each_part_and_the_recent_events(self):
        win = self.make()
        r = self.root
        r.health.tv_poll(True, 9)
        r.health.tv_playing("play", "Playlet")
        r.health.reload(False, "load abc at 5s")
        r.health.app_late(2.0)
        with mock.patch.object(g.Remote, "_open", return_value=b"{}"):
            win.refresh()
        self.assertIn("9 ms", self.text(win, "tv"))
        self.assertIn("Playlet", self.text(win, "playlet"))
        self.assertIn("1 reloads", self.text(win, "sync"))
        self.assertIn("this app 1", self.text(win, "stalls"))
        self.assertIn("load abc at 5s", win.recent.cget("text"))
        self.assertEqual(self.text(win, "audio"), "Not connected")

    def test_an_unreachable_tv_is_red_and_says_what_to_check(self):
        win = self.make()
        self.root.health.tv_poll(False)
        self.root.health.tv_poll(False)
        win.refresh()
        self.assertIn("Not answering", self.text(win, "tv"))

    def test_the_audio_row_reports_the_buffer_and_whether_losses_are_concealed(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        buf = g.AudioBuffer(lambda n: b"\0" * n, 300, stop)
        ln = SimpleNamespace(stats=g.RtpStats(), buffer=buf, concealing=False, last_report="")
        ln.stats.lost, ln.stats.gaps = 6, 1
        color, text = g.App.audio_health(SimpleNamespace(listener=ln))
        self.assertIn("6 packets lost in 1 gaps", text)
        self.assertIn("underruns", text)
        self.assertIn("no libopus", text)

    def test_copy_report_puts_the_text_on_the_clipboard(self):
        win = self.make()
        self.root.health.reload(False, "load xyz")
        put = []
        win.clipboard_clear = lambda: None                      # never touch the real clipboard from a test
        win.clipboard_append = put.append
        win.copy()
        self.assertIn("load xyz", put[0])


if __name__ == "__main__":
    unittest.main()
