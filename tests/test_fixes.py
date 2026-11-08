"""Regression tests for the bugs found in the review: audio thread survival, device failures, Web cast cleanup."""
import json
import os
import socket
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import roku_listen_gui as g  # noqa: E402
import web_tab  # noqa: E402
from test_web_tab_cast import Base  # noqa: E402
from stream_finder import Candidate  # noqa: E402


def rtp(seq):
    return bytes([0x80, 0x61]) + seq.to_bytes(2, "big") + bytes(8)


class RtpCounting(unittest.TestCase):
    def feed(self, seqs):
        st = g.RtpStats()
        for i, s in enumerate(seqs):
            st.packet(s, i * 0.02)
        return st

    def test_a_clean_run_has_no_gaps(self):
        st = self.feed(range(100, 150))
        self.assertEqual((st.packets, st.lost, st.gaps), (50, 0, 0))

    def test_missing_packets_are_counted_by_sequence_number(self):
        st = self.feed([1, 2, 3, 9, 10, 11, 14])
        self.assertEqual((st.lost, st.gaps), (5 + 2, 2))

    def test_the_sequence_number_wraps_around(self):
        st = self.feed([65534, 65535, 0, 1])
        self.assertEqual((st.lost, st.gaps), (0, 0))

    def test_late_and_duplicate_packets_are_not_losses(self):
        st = self.feed([1, 2, 4, 3, 5, 5])
        self.assertEqual(st.reordered, 2)
        self.assertEqual(st.lost, 1)

    def test_the_report_resets_the_counts(self):
        st = self.feed([1, 2, 5])
        self.assertIn("2 missing in 1 gaps", st.report())
        self.assertEqual((st.packets, st.lost), (0, 0))


class FakeSock:
    """A UDP socket that plays a script: exceptions are raised, bytes are returned, and the loop is stopped at the end."""

    def __init__(self, script, stop):
        self.script, self.stop, self.sent = list(script), stop, []

    def settimeout(self, t):
        pass

    def recvfrom(self, n):
        if not self.script:
            self.stop.set()
            raise socket.timeout()
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item, ("10.0.0.5", 5150)

    def sendto(self, data, addr):
        self.sent.append(addr)

    def close(self):
        pass


class AudioThread(unittest.TestCase):
    def listener(self, script, statuses):
        ln = g.Listener("10.0.0.5", 500, lambda text, ended=False: statuses.append(text))
        ln.sock = FakeSock(script, ln.stop_evt)
        return ln

    def test_a_windows_connection_reset_does_not_end_the_audio(self):
        statuses = []
        ln = self.listener([ConnectionResetError(10054, "reset"), rtp(1), ConnectionResetError(), rtp(2)], statuses)
        ln._rtp_loop()
        self.assertEqual(statuses, ["Connected - playing"])      # it kept going past both resets and saw the packets
        self.assertEqual(ln.sock.script, [])

    def test_a_failed_send_to_the_roku_does_not_end_it_either(self):
        statuses = []
        ln = self.listener([rtp(1), rtp(2)], statuses)
        ln.sock.sendto = mock.Mock(side_effect=OSError("network down"))
        ln._rtp_loop()
        self.assertEqual(statuses, ["Connected - playing"])

    def test_silence_is_said_out_loud_and_cleared_when_audio_returns(self):
        statuses = []

        def pause():
            time.sleep(0.12)
            raise socket.timeout()
        ln = self.listener([rtp(1), pause, pause, rtp(2)], statuses)
        with mock.patch.object(g, "SILENCE_AFTER", 0.05):
            ln._rtp_loop()
        self.assertEqual(statuses, ["Connected - playing", "No audio from the Roku", "Connected - playing"])


class OutputDevice(unittest.TestCase):
    DEVICES = [{"name": "Speakers", "max_output_channels": 2, "hostapi": 0},
               {"name": "Mic", "max_output_channels": 0, "hostapi": 0},
               {"name": "Speakers", "max_output_channels": 2, "hostapi": 1},
               {"name": "Headset", "max_output_channels": 2, "hostapi": 1}]

    def test_outputs_are_listed_once_and_inputs_are_left_out(self):
        with mock.patch.object(g.sd, "query_devices", return_value=self.DEVICES):
            self.assertEqual(g.output_devices(), ["Speakers", "Headset"])

    def test_a_saved_device_that_is_gone_falls_back_to_the_default(self):
        with mock.patch.object(g.sd, "query_devices", side_effect=lambda i=None: self.DEVICES if i is None else self.DEVICES[i]), \
                mock.patch.object(g.sd, "default", SimpleNamespace(hostapi=1)):
            self.assertIsNone(g.find_output_device(""))
            self.assertIsNone(g.find_output_device("Unplugged DAC"))
            self.assertEqual(g.find_output_device("Headset"), 3)
            self.assertEqual(g.find_output_device("Speakers"), 2)    # the copy on the default audio API

    def test_a_lost_device_is_reopened_and_the_session_survives(self):
        statuses, opened = [], []

        class Out:
            def __init__(self, **kw):
                opened.append(kw["device"])
                if len(opened) == 1:
                    raise g.sd.PortAudioError("There is no driver installed on your system.")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, buf):
                ln.stop_evt.set()                                # one buffer played: done

        ln = g.Listener("10.0.0.5", 500, lambda text, ended=False: statuses.append(text))
        ln.proc = SimpleNamespace(stdout=SimpleNamespace(read=lambda n: b"\0" * n), poll=lambda: None)
        with mock.patch.object(g.sd, "RawOutputStream", Out), mock.patch.object(g.Listener, "_rescan_devices"):
            ln.stop_evt.wait = lambda t=None: False              # no real 1.5 s pause
            ln._audio_loop()
        self.assertEqual(len(opened), 2)                         # the second try worked
        self.assertFalse(ln.failed)
        self.assertEqual(statuses[0], "Audio device lost, reconnecting…")

    def test_a_device_that_never_comes_back_fails_after_the_retry_window(self):
        statuses = []

        def broken(**kw):
            raise g.sd.PortAudioError("There is no driver installed on your system.")
        ln = g.Listener("10.0.0.5", 500, lambda text, ended=False: statuses.append((text, ended)))
        ln.stop_evt.wait = lambda t=None: False
        with mock.patch.object(g.sd, "RawOutputStream", broken), mock.patch.object(g.Listener, "_rescan_devices"), \
                mock.patch.object(g, "DEVICE_RETRY", 0.0):
            ln._audio_loop()
        self.assertTrue(ln.failed)
        self.assertTrue(statuses[-1][0].startswith("Audio error"))

    def test_changing_output_while_connected_reopens_the_stream_on_the_new_device(self):
        opened = []

        class Out:
            def __init__(self, **kw):
                opened.append(kw["device"])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, buf):
                if len(opened) == 1:
                    ln.device, ln.reopen = "Headset", True          # what picking another output does
                else:
                    ln.stop_evt.set()

        ln = g.Listener("10.0.0.5", 500, lambda *a, **k: None)
        ln.proc = SimpleNamespace(stdout=SimpleNamespace(read=lambda n: b"\0" * n), poll=lambda: None)
        with mock.patch.object(g.sd, "RawOutputStream", Out), \
                mock.patch.object(g, "find_output_device", side_effect=lambda name: 3 if name else None):
            ln._audio_loop()
        self.assertEqual(opened, [None, 3])


class CastCleanup(Base):
    def setUp(self):
        super().setUp()
        self.stopped = []
        self.tab.relay.stop_upscaling = lambda: self.stopped.append(1)
        self.tab.guard.page_images = lambda *a: []
        self.tab.guard.page_info = lambda *a: {}
        self.tab.discord = None
        self.cand = Candidate(url="https://cdn/ep.m3u8", kind="hls")

    def test_a_cast_that_fails_after_the_encoder_started_stops_it(self):
        attempt = self.begin()
        self.tab.upscale_owner, self.tab.upscaled = attempt, {"mode": "fast"}
        with mock.patch.object(self.tab, "_send_cast", side_effect=web_tab.CastError("The TV never asked")):
            self.run_worker(attempt, self.tab._send, self.cand, "Ep")
        self.assertEqual(self.stopped, [1])
        self.assertIsNone(self.tab.upscaled)

    def test_a_cancelled_cast_stops_its_encoder(self):
        attempt = self.begin()
        self.tab.upscale_owner, self.tab.upscaled = attempt, {"mode": "fast"}
        with mock.patch.object(self.tab, "_send_cast", side_effect=web_tab.Cancelled()):
            self.run_worker(attempt, self.tab._send, self.cand, "Ep")
        self.assertEqual(self.stopped, [1])

    def test_an_old_attempt_never_stops_a_newer_casts_encoder(self):
        old = self.begin()
        newer = self.begin()
        self.tab.upscale_owner, self.tab.upscaled = newer, {"mode": "fast"}
        with mock.patch.object(self.tab, "_send_cast", side_effect=web_tab.Cancelled()):
            self.run_worker(old, self.tab._send, self.cand, "Ep")
        self.assertEqual(self.stopped, [])
        self.assertIsNotNone(self.tab.upscaled)

    def test_a_cast_that_never_upscaled_leaves_the_relay_alone(self):
        attempt = self.begin()
        with mock.patch.object(self.tab, "_send_cast", side_effect=web_tab.CastError("x")):
            self.run_worker(attempt, self.tab._send, self.cand, "Ep")
        self.assertEqual(self.stopped, [])

    def test_following_uses_the_current_browser_after_a_restart(self):
        old, new = object(), object()
        self.sess.hits = 1
        self.tab.guard = SimpleNamespace(**{**self.tab.guard.__dict__, "marker": new})
        self.tab.sync = SimpleNamespace(start=mock.Mock(), active=False, lead=3.0, guard=old)
        self.run_worker(self.begin(), self.tab._send, self.cand, "Ep")
        self.assertIs(self.tab.sync.guard, self.tab.guard)       # not the guard of the browser that was closed
        self.tab.sync.start.assert_called_once()


class LeftOverCast(Base):
    """The page was left (or a recast failed) and the TV has since gone elsewhere."""

    def setUp(self):
        super().setUp()
        self.cleared, self.stopped = [], threading.Event()
        self.tab.discord = SimpleNamespace(showing="web", clear=lambda s: self.cleared.append(s),
                                           is_on=lambda s: True)
        self.tab.relay.stop_upscaling = self.stopped.set
        self.tab.upscaled, self.tab.tv_gone = {"mode": "hq"}, 0

    PLAYING = {"state": "play", "app": "RokuCast", "pos": 5, "dur": 100}
    HOME = {"state": "none", "app": "", "pos": 0, "dur": 0}

    def test_the_tv_leaving_clears_the_status_and_stops_the_encoder_after_two_polls(self):
        self.tab.tv_state(self.HOME)
        self.assertEqual(self.cleared, [])                       # one odd reading isn't enough
        self.tab.tv_state(self.HOME)
        self.assertEqual(self.cleared, ["web"])
        self.assertTrue(self.stopped.wait(2))
        self.assertIsNone(self.tab.upscaled)

    def test_the_tv_still_playing_the_cast_keeps_everything(self):
        for _ in range(5):
            self.tab.tv_state(self.PLAYING)
        self.assertEqual(self.cleared, [])
        self.assertIsNotNone(self.tab.upscaled)

    def test_another_app_on_screen_counts_as_gone(self):
        other = {"state": "play", "app": "Playlet", "pos": 5, "dur": 100}
        self.tab.tv_state(other)
        self.tab.tv_state(other)
        self.assertEqual(self.cleared, ["web"])

    def test_an_unreachable_tv_is_not_evidence_of_anything(self):
        for _ in range(5):
            self.tab.tv_state(None)
        self.assertEqual(self.cleared, [])

    def test_a_blip_between_readings_resets_the_count(self):
        self.tab.tv_state(self.HOME)
        self.tab.tv_state(self.PLAYING)
        self.tab.tv_state(self.HOME)
        self.assertEqual(self.cleared, [])

    def test_while_the_page_is_being_followed_websync_is_in_charge(self):
        self.tab.sync = SimpleNamespace(active=True, live=False, settle_until=0, offset=0, dur=0)
        for _ in range(4):
            self.tab.tv_state(self.HOME)
        self.assertEqual(self.cleared, [])

    def test_nothing_left_over_means_nothing_to_do(self):
        self.tab.upscaled, self.tab.discord.showing = None, None
        for _ in range(4):
            self.tab.tv_state(self.HOME)
        self.assertEqual(self.cleared, [])


class StreamHealth(Base):
    def setUp(self):
        super().setUp()
        self.tab.sync = SimpleNamespace(active=True)
        self.tab.attempt, self.tab.cast_session = 3, None
        self.notes = []
        self.tab._note = lambda text, color=None, seconds=8: self.notes.append(text)
        self.tab._post = lambda fn: fn()

    def watch(self, sess, until, attempt=3):
        t = threading.Thread(target=self.tab._watch_health, args=(attempt, sess, 0.01))
        t.start()
        until()
        self.tab.sync.active = False
        t.join(5)
        self.assertFalse(t.is_alive())

    def wait_for_note(self, text):
        end = time.time() + 5
        while time.time() < end and not any(text in n for n in self.notes):
            time.sleep(0.01)

    def test_failures_long_after_the_first_half_minute_are_still_reported(self):
        sess = SimpleNamespace(failures=[], error="", stalls=0)

        def later():
            time.sleep(0.1)                                      # the old watcher would have stopped by now
            sess.failures += [(403, "a.ts"), (403, "b.ts"), (403, "c.ts")]
            self.wait_for_note("failing to load")
        self.watch(sess, later)
        self.assertTrue(any("failing to load (HTTP 403)" in n for n in self.notes))

    def test_a_scrub_moves_the_watch_to_the_new_session(self):
        first = SimpleNamespace(failures=[], error="", stalls=0)
        second = SimpleNamespace(failures=[], error="", stalls=0)

        def scrub():
            time.sleep(0.05)
            self.tab.cast_session = second
            time.sleep(0.05)
            second.failures += [(0, "a.m4s"), (0, "b.m4s"), (0, "c.m4s")]
            self.wait_for_note("failing to load")
        self.watch(first, scrub)
        self.assertTrue(any("failing to load" in n for n in self.notes))

    def test_a_dead_encoder_is_reported(self):
        sess = SimpleNamespace(failures=[], error="FFmpeg exited with code 1", stalls=0)
        self.watch(sess, lambda: self.wait_for_note("The stream stopped"))
        self.assertTrue(any("The stream stopped: FFmpeg exited" in n for n in self.notes))

    def test_a_slow_encoder_is_reported_once(self):
        sess = SimpleNamespace(failures=[], error="", stalls=3)
        self.tab.upscaled = {"mode": "hq"}
        self.watch(sess, lambda: (self.wait_for_note("slower than the video"), time.sleep(0.1)))
        self.assertEqual(sum("slower than the video" in n for n in self.notes), 1)

    def test_a_newer_cast_ends_the_old_watch(self):
        sess = SimpleNamespace(failures=[], error="", stalls=0)
        t = threading.Thread(target=self.tab._watch_health, args=(2, sess, 0.01))     # attempt 2; the tab is on 3
        t.start()
        t.join(2)
        self.assertFalse(t.is_alive())


class YouTubeStatus(unittest.TestCase):
    def app(self):
        calls = []
        discord = SimpleNamespace(showing="youtube", client_id="1", enabled={}, calls=calls,
                                  show=lambda *a, **k: calls.append(("show", a, k)), is_on=lambda s: True,
                                  progress=lambda *a, **k: None,
                                  clear=lambda s: calls.append(("clear", s)))
        app = SimpleNamespace(discord=discord, _yt_since=0.0, _yt_gone=0, _yt_id=None, _yt_want=None)
        app._yt_show = lambda title, vid: g.App._yt_show(app, title, vid)
        app._yt_now = lambda title, vid: g.App._yt_now(app, title, vid)
        app._adopt_playlet = lambda: g.App._adopt_playlet(app)
        app._yt_adopt = lambda: g.App._yt_adopt(app)
        app._host = "tv"
        app.ADOPT_EVERY = g.App.ADOPT_EVERY
        return app

    def adopt(self, app, st, queue):
        """on_tv_state with every thread run on the spot and Playlet's queue faked."""
        class Inline:
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
        with mock.patch.object(g.threading, "Thread", Inline), \
                mock.patch.object(g.Remote, "playlet_now", return_value=queue), \
                mock.patch.object(g.discord_presence, "youtube_info", return_value=("Chan", "Name")):
            g.App.on_tv_state(app, st)

    PLAYING = {"state": "play", "app": "Playlet", "pos": 5, "dur": 90}

    def test_a_video_started_from_the_phone_gets_a_status(self):
        """The phone's YouTube tab (and a YouTube link sent from the phone) play in Playlet without the Chrome extension,
        so CastSync never announced them."""
        app = self.app()
        app.discord.showing = None
        self.adopt(app, self.PLAYING, {"videoid": "phone1", "title": "From the phone"})
        kind, args, _ = app.discord.calls[0]
        self.assertEqual((kind, args[:3]), ("show", ("youtube", "Chan", "Name")))
        self.assertEqual(args[4], "https://i.ytimg.com/vi/phone1/hqdefault.jpg")

    def test_a_different_video_started_from_the_phone_replaces_the_status(self):
        app = self.app()
        self.now(app, "A", "old")
        app.discord.calls.clear()
        self.adopt(app, self.PLAYING, {"videoid": "new", "title": "B"})
        self.assertEqual(len(app.discord.calls), 1)
        self.assertEqual(app._yt_id, "new")

    def test_a_video_the_status_already_shows_is_not_announced_again(self):
        app = self.app()
        self.now(app, "A", "same")
        app.discord.calls.clear()
        self.adopt(app, self.PLAYING, {"videoid": "same", "title": "A"})
        self.assertEqual(app.discord.calls, [])

    def test_playlet_is_asked_only_every_few_seconds(self):
        app = self.app()
        app.discord.showing = None
        with mock.patch.object(g.Remote, "playlet_now", return_value={}) as asked:
            class Inline:
                def __init__(self, target, args=(), daemon=None):
                    self.target = target

                def start(self):
                    self.target()
            with mock.patch.object(g.threading, "Thread", Inline):
                for _ in range(5):
                    g.App.on_tv_state(app, self.PLAYING)
        self.assertEqual(asked.call_count, 1)

    def test_nothing_is_adopted_when_the_tv_is_not_in_playlet_or_the_switch_is_off(self):
        app = self.app()
        app.discord.showing = None
        self.adopt(app, {"state": "play", "app": "RokuCast", "pos": 1, "dur": 9}, {"videoid": "x"})
        app.discord.is_on = lambda s: False
        self.adopt(app, self.PLAYING, {"videoid": "x"})
        self.assertEqual(app.discord.calls, [])

    def test_playlet_not_answering_leaves_things_alone(self):
        app = self.app()
        app.discord.showing = None
        self.adopt(app, self.PLAYING, {})
        self.assertEqual(app.discord.calls, [])

    def now(self, app, title, vid):
        """_yt_now with its look-up thread run on the spot and YouTube's answer faked."""
        class Inline:
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
        with mock.patch.object(g.threading, "Thread", Inline),                 mock.patch.object(g.discord_presence, "youtube_info", return_value=("Some Channel", "Some Video")):
            g.App._yt_now(app, title, vid)

    def test_a_video_shows_its_title_and_thumbnail(self):
        app = self.app()
        self.now(app, "Some Video", "abc123")
        kind, args, _ = app.discord.calls[0]
        self.assertEqual(args[:3], ("youtube", "Some Channel", "Some Video"))
        self.assertEqual(args[4], "https://i.ytimg.com/vi/abc123/hqdefault.jpg")

    def test_the_timer_restarts_for_a_new_video_but_not_for_the_same_one(self):
        app = self.app()
        self.now(app, "A", "id1")
        first = app._yt_since
        time.sleep(0.02)
        self.now(app, "A", "id1")
        self.assertEqual(app._yt_since, first)
        self.now(app, "B", "id2")
        self.assertGreater(app._yt_since, first)

    def test_leaving_playlet_takes_it_down(self):
        app = self.app()
        g.App._yt_now(app, None, None)
        self.assertEqual(app.discord.calls, [("clear", "youtube")])

    def test_the_tv_going_elsewhere_clears_it_after_two_polls(self):
        app = self.app()
        away = {"state": "none", "app": "", "pos": 0, "dur": 0}
        g.App.on_tv_state(app, away)
        self.assertEqual(app.discord.calls, [])
        g.App.on_tv_state(app, away)
        self.assertEqual(app.discord.calls, [("clear", "youtube")])

    def test_playlet_still_playing_keeps_it(self):
        app = self.app()
        self.now(app, "A", "id1")
        app.discord.calls.clear()
        for _ in range(4):
            self.adopt(app, {"state": "play", "app": "Playlet", "pos": 1, "dur": 9}, {"videoid": "id1"})
        self.assertEqual(app.discord.calls, [])

    def test_castsync_announces_what_it_puts_on_the_tv(self):
        seen = []
        sync = g.CastSync.__new__(g.CastSync)
        sync.on_now = lambda title, vid: seen.append((title, vid))
        sync._announce("T", "v1")
        sync._announce(None, None)
        self.assertEqual(seen, [("T", "v1"), (None, None)])
        sync.on_now = lambda *a: 1 / 0                           # a broken callback must not break casting
        sync._announce("T", "v1")

    def _sync(self, current="v1"):
        sync = g.CastSync.__new__(g.CastSync)
        sync.current, sync.title, sync.q = current, "", __import__("queue").Queue()
        sync.on_now, sync.settle_until, sync.lead, sync.last_check = None, 0.0, 2.5, 0.0
        sync._last_seek, sync.bridge, sync.host_fn = None, SimpleNamespace(player=lambda: None), lambda: "tv"
        return sync

    def test_a_repeated_load_does_not_reload_a_video_the_tv_is_already_playing(self):
        sync = self._sync()
        playing = {"state": "play", "app": "Playlet", "pos": 330.0, "dur": 900}
        with mock.patch.object(g.Remote, "state", return_value=playing),              mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "v1"}),              mock.patch.object(g.Remote, "cast") as cast:
            sync._handle({"kind": "load", "id": "v1", "t": 325})
        cast.assert_not_called()

    def test_a_load_for_a_different_point_or_video_still_reloads(self):
        for ev, state in (({"kind": "load", "id": "v1", "t": 10}, {"state": "play", "app": "Playlet", "pos": 330.0, "dur": 900}),
                          ({"kind": "load", "id": "v2", "t": 330}, {"state": "play", "app": "Playlet", "pos": 330.0, "dur": 900}),
                          ({"kind": "load", "id": "v1", "t": 330}, {"state": "play", "app": "Netflix", "pos": 330.0, "dur": 900})):
            sync = self._sync()
            with mock.patch.object(g.Remote, "state", return_value=state),                  mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "v1"}),                  mock.patch.object(g.Remote, "cast") as cast,                  mock.patch.object(sync, "_started", return_value=False):
                sync._handle(ev)
            cast.assert_called_once()

    def test_a_slow_playlet_answer_is_not_sent_twice(self):
        calls = []

        def slow(*a, **k):
            calls.append(k.get("timeout"))
            raise TimeoutError("timed out")
        with mock.patch.object(g.Remote, "_open", side_effect=slow),              mock.patch.object(g.Remote, "_ensure_playlet") as ensure:
            with self.assertRaises(TimeoutError):
                g.Remote.cast("tv", "v1", 5)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], 8)
        ensure.assert_not_called()

    def test_the_extension_waits_out_a_stalled_pc_before_handing_youtube_back(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "feed.js"),
                   encoding="utf-8").read()
        ms = int(__import__("re").search(r"APP_SILENT_MS = (\d+)", src).group(1))
        self.assertGreaterEqual(ms, 10000)
        self.assertIn("lastReply > APP_SILENT_MS", src)


class BrowserStandIn:
    """A WebGuard whose browser is answered by a stand-in."""

    def guard(self, evaluated, raw=False):
        """evaluated: what the page script answers. For the picture search, a single address stands in for 'the page's
        first preview tag'; raw=True passes the text through untouched."""
        from web_guard import WebGuard
        if not raw and evaluated is not None and not evaluated.startswith("{"):
            evaluated = json.dumps({"meta": [evaluated] if evaluated else [], "hero": []})
        guard = WebGuard()
        self.addCleanup(guard._stop.set)

        def browser():
            cmd, arg = guard._cmds.get(timeout=3)
            if cmd == "eval" and evaluated is not None:
                arg[1]({"result": {"type": "string", "value": evaluated}})
        threading.Thread(target=browser, daemon=True).start()
        return guard



class PageImage(BrowserStandIn, unittest.TestCase):
    """WebGuard.page_image(s): the page's own pictures for the Discord status."""

    def video(self, guard, poster, dur=1400.0, age=0.0):
        guard._videos[("s", "f")] = {"at": time.time() - age, "items": [{"poster": poster, "dur": dur}]}

    def test_the_pages_preview_image_wins(self):
        guard = self.guard("https://cdn.example/ep.jpg")
        self.video(guard, "https://cdn.example/poster.jpg")
        self.assertEqual(guard.page_image(2.0), "https://cdn.example/ep.jpg")

    def test_a_video_poster_is_the_fallback(self):
        guard = self.guard("")
        self.video(guard, "https://cdn.example/poster.jpg")
        self.assertEqual(guard.page_image(2.0), "https://cdn.example/poster.jpg")

    def test_the_poster_must_belong_to_the_video_being_cast(self):
        guard = self.guard("")
        self.video(guard, "https://cdn.example/other.jpg", dur=30.0)         # an ad's poster, not the 1400 s episode
        self.assertEqual(guard.page_image(2.0, 1400.0), "")

    def test_insecure_and_stale_pictures_are_ignored(self):
        guard = self.guard("http://cdn.example/ep.jpg")
        self.video(guard, "http://cdn.example/poster.jpg")
        self.assertEqual(guard.page_image(2.0), "")
        guard = self.guard("")
        self.video(guard, "https://cdn.example/poster.jpg", age=60.0)
        self.assertEqual(guard.page_image(2.0), "")

    def test_a_logo_picture_gives_way_to_the_poster(self):
        guard = self.guard("https://cdn.example/site-logo.png")
        self.video(guard, "https://cdn.example/poster.jpg")
        self.assertEqual(guard.page_image(2.0), "https://cdn.example/poster.jpg")

    def test_a_site_icon_in_the_first_tag_does_not_hide_the_real_picture_in_a_later_one(self):
        """Miruro's pages carry the site icon as the first og:image and the show's artwork as a later one."""
        page = {"meta": ["https://miruro.cx/icon-output.png", "https://s4.anilist.co/banner/43.jpg"],
                "hero": ["https://image.tmdb.org/t/p/original/hero.jpg"]}
        guard = self.guard(json.dumps(page))
        self.video(guard, "https://cdn.example/poster.jpg")
        self.assertEqual(guard.page_images(2.0), ["https://s4.anilist.co/banner/43.jpg", "https://cdn.example/poster.jpg",
                                                  "https://image.tmdb.org/t/p/original/hero.jpg"])

    def test_a_browser_that_never_answers_costs_only_the_timeout(self):
        guard = self.guard(None)
        began = time.time()
        self.assertEqual(guard.page_image(0.3), "")
        self.assertLess(time.time() - began, 2.0)


if __name__ == "__main__":
    unittest.main()


class PageInfo(BrowserStandIn, unittest.TestCase):
    """WebGuard.page_info and the Web tab's choice of name, site and picture for the Discord status."""

    def test_the_live_title_and_site_come_back(self):
        guard = self.guard('{"url": "https://miruro.cx/w/1", "title": "Frieren - Miruro", "og_title": "", "site": "Miruro"}', raw=True)
        self.assertEqual(guard.page_info(2.0)["title"], "Frieren - Miruro")

    def test_garbage_or_silence_is_just_empty(self):
        self.assertEqual(self.guard("not json", raw=True).page_info(2.0), {})
        self.assertEqual(self.guard(None).page_info(0.3), {})



class Describe(unittest.TestCase):
    def tab(self, info, image="https://cdn.example/ep.jpg"):
        tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.cast_page = "https://www.miruro.cx/watch/1"
        tab.guard = SimpleNamespace(page_info=lambda t: info, page_images=lambda t, d: [image] if image else [])
        tab.ui = SimpleNamespace(log=lambda m: None)
        patch = mock.patch.object(web_tab.discord_presence, "pick_image", lambda urls, **k: urls[0] if urls else "")
        patch.start()
        self.addCleanup(patch.stop)
        return tab

    cand = Candidate(url="https://cdn.example/a/ep.m3u8", kind="hls")

    def test_it_names_the_show_the_site_and_the_picture(self):
        tab = self.tab({"url": "https://www.miruro.cx/watch/1#x", "title": "Watch Frieren Episode 5 English Sub - Miruro"})
        self.assertEqual(tab._describe("stale title", self.cand),
                         ("Frieren Episode 5", "Miruro", "https://cdn.example/ep.jpg"))

    def test_a_browser_that_does_not_answer_falls_back_to_the_title_from_the_click(self):
        tab = self.tab({})
        self.assertEqual(tab._describe("Heat | Miruro", self.cand), ("Heat", "Miruro", "https://cdn.example/ep.jpg"))

    def test_a_page_that_has_moved_on_is_not_described_by_its_new_title_or_picture(self):
        tab = self.tab({"url": "https://www.miruro.cx/home", "title": "Home - Miruro"})
        self.assertEqual(tab._describe("Frieren - Miruro", self.cand), ("Frieren", "Miruro", ""))

    def test_no_title_at_all_falls_back_to_the_streams_host(self):
        tab = self.tab({})
        self.assertEqual(tab._describe("", self.cand)[0], "cdn.example")



class YoutubePipeline(unittest.TestCase):
    """The YouTube tab -> Playlet pipeline: playlists went back to the first song, and a 2600-entry queue made every
    request to the TV slow."""

    def sync(self, current="v1"):
        with mock.patch.object(g.threading, "Thread"):
            s = g.CastSync(lambda: "tv", SimpleNamespace(player=lambda: None), None)
        s.current = current
        return s

    PLAYING = {"state": "play", "app": "Playlet", "pos": 40.0, "dur": 900}

    # ---- what is sent to Playlet ---------------------------------------------------------------
    def cast(self, *a, **k):
        sent = []
        with mock.patch.object(g.Remote, "_open", side_effect=lambda url, data=None, **kw: sent.append(
                (kw.get("method") or ("POST" if data is not None else "GET"), url, data))):
            g.Remote.cast("tv", *a, **k)
        return sent

    def test_a_cast_is_one_video_and_never_a_playlist(self):
        sent = self.cast("v2", 30, fresh=True)
        play = [s for s in sent if s[1].endswith("/api/queue/play")][0]
        self.assertEqual(json.loads(play[2]), {"videoId": "v2", "timestamp": 30})

    def test_a_new_video_starts_from_an_empty_queue(self):
        sent = self.cast("v2", 0, fresh=True)
        self.assertEqual([(m, u.rsplit("/api/", 1)[1]) for m, u, _ in sent], [("DELETE", "queue"), ("POST", "queue/play")])

    def test_a_seek_inside_a_video_keeps_the_queue(self):
        self.assertEqual([m for m, *_ in self.cast("v1", 90)], ["POST"])

    def test_a_queue_that_cannot_be_cleared_does_not_stop_the_cast(self):
        calls = []

        def fake(url, data=None, **kw):
            calls.append(kw.get("method") or "POST")
            if kw.get("method") == "DELETE":
                raise TimeoutError("slow")
        with mock.patch.object(g.Remote, "_open", side_effect=fake):
            g.Remote.cast("tv", "v2", 0, fresh=True)
        self.assertEqual(calls, ["DELETE", "POST"])

    def test_the_phone_casts_the_linked_video_not_its_playlist(self):
        backend = g.PhoneBackend.__new__(g.PhoneBackend)
        backend.app = SimpleNamespace(_host="tv")
        with mock.patch.object(g.Remote, "cast") as cast:
            backend.cast("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghijklmnop&t=30")
        cast.assert_called_once_with("tv", "dQw4w9WgXcQ", 30, fresh=True)

    def test_the_extension_sends_no_playlist_and_flags_a_page_that_only_just_started_following(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "feed.js"),
                   encoding="utf-8").read()
        self.assertNotIn("list:", src)
        self.assertIn("castNow(v, true)", src)

    # ---- clicking through songs ---------------------------------------------------------------------
    def drain(self, s, events):
        for e in events:
            s.q.put(e)
        out = []
        while not s.q.empty():
            out.append(s._next())
        return out

    def test_a_load_that_a_newer_load_replaces_is_skipped(self):
        s = self.sync()
        out = self.drain(s, [{"kind": "load", "id": "a"}, {"kind": "load", "id": "b"}, {"kind": "load", "id": "c"}])
        self.assertEqual([e["id"] for e in out], ["c"])

    def test_events_about_a_skipped_video_are_ignored_not_sent_to_the_tv(self):
        s = self.sync(current="old")
        events = self.drain(s, [{"kind": "load", "id": "a"}, {"kind": "pause", "id": "a"}, {"kind": "load", "id": "b"}])
        with mock.patch.object(g.Remote, "state", return_value=self.PLAYING), \
                mock.patch.object(g.Remote, "playlet_now", return_value={}), \
                mock.patch.object(g.Remote, "cast") as cast, mock.patch.object(g.Remote, "key") as key, \
                mock.patch.object(s, "_started", return_value=False):
            for e in events:
                s._handle(e)
        self.assertEqual([c.args[1] for c in cast.call_args_list], ["b"])
        key.assert_not_called()

    def test_leaving_for_the_next_song_is_not_a_trip_home(self):
        s = self.sync()
        out = self.drain(s, [{"kind": "leave", "id": "a"}, {"kind": "load", "id": "b"}])
        self.assertEqual([e["kind"] for e in out], ["load"])

    def test_a_lone_leave_and_the_scrub_coalescing_still_work(self):
        s = self.sync()
        self.assertEqual([e["kind"] for e in self.drain(s, [{"kind": "leave", "id": "a"}])], ["leave"])
        out = self.drain(s, [{"kind": "seek", "id": "a", "t": 1}, {"kind": "seek", "id": "a", "t": 2}, {"kind": "pause", "id": "a"}])
        self.assertEqual([(e["kind"], e.get("t")) for e in out], [("seek", 2), ("pause", None)])

    def test_a_seek_for_a_video_the_tv_is_not_on_is_ignored(self):
        s = self.sync(current="v1")
        with mock.patch.object(g.Remote, "cast") as cast, mock.patch.object(g.Remote, "key") as key:
            s._handle({"kind": "seek", "id": "other", "t": 50})
            s._handle({"kind": "play", "id": "other", "t": 50})
        cast.assert_not_called()
        key.assert_not_called()

    # ---- the TV is already on it -------------------------------------------------------------------
    def test_a_video_the_tv_moved_to_by_itself_is_not_reloaded(self):
        s = self.sync(current="old")
        just_started = {"state": "play", "app": "Playlet", "pos": 3.0, "dur": 200}
        with mock.patch.object(g.Remote, "state", return_value=just_started), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "next"}), \
                mock.patch.object(g.Remote, "cast") as cast, mock.patch.object(s, "_align"):
            s._handle({"kind": "load", "id": "next", "t": 2})
        cast.assert_not_called()
        self.assertEqual(s.current, "next")

    def test_a_page_that_only_started_following_never_drags_the_tv_back(self):
        """The page was reloaded at 7 s while the TV was at 40 s: 33 s apart, so this used to reload the TV at 7 s."""
        for resume, reloads in ((True, False), (False, True)):
            s = self.sync()
            with mock.patch.object(g.Remote, "state", return_value=self.PLAYING), \
                    mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "v1"}), \
                    mock.patch.object(g.Remote, "cast") as cast, mock.patch.object(s, "_align"), \
                    mock.patch.object(s, "_started", return_value=False):
                s._handle({"kind": "load", "id": "v1", "t": 7, "resume": resume})
            self.assertEqual(cast.called, reloads, resume)

    def test_the_pc_does_not_chase_the_clock_of_a_different_video(self):
        s = self.sync()
        moved = []
        s.bridge = SimpleNamespace(player=lambda: {"id": "v1", "t": 5.0, "paused": False}, send=moved.append)
        with mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "other"}), \
                mock.patch.object(s, "_tv_position", return_value=300.0):
            s._align("tv")
        self.assertEqual(moved, [])
        with mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "v1"}), \
                mock.patch.object(s, "_tv_position", return_value=300.0):
            s._align("tv")
        self.assertEqual(len(moved), 1)
