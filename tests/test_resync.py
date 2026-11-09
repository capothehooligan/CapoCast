"""The Resync button (the PC's YouTube player vs the TV), and the other refinements that came with it: the extension's
new commands, the phone's button, the remembered window, Find my Roku, and the new widgets."""
import json
import os
import sys
import time
import tkinter as tk
import unittest
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phone_remote as pr  # noqa: E402
import roku_find  # noqa: E402
import roku_listen_gui as g  # noqa: E402
import ui_kit  # noqa: E402
from yt_bridge import Bridge  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tv(state="play", pos=100.0, dur=600.0, app="Playlet"):
    return {"state": state, "app": app, "pos": pos, "dur": dur}


def pc(vid="abc", t=92.0, paused=False, ad=False):
    return {"id": vid, "t": t, "paused": paused, "ad": ad}


class Plan(unittest.TestCase):
    def test_both_on_the_same_video_means_line_the_pc_up(self):
        self.assertEqual(g.resync_plan(tv(), "abc", pc(), "abc")[0], "align")
        self.assertEqual(g.resync_plan(tv("pause"), "abc", pc(), "abc")[0], "align")

    def test_an_unknown_tv_video_falls_back_to_the_one_we_last_cast(self):
        self.assertEqual(g.resync_plan(tv(), "", pc(), "abc")[0], "align")

    def test_a_buffering_tv_is_waited_for(self):
        self.assertEqual(g.resync_plan(tv("buffer"), "abc", pc(), "abc")[0], "buffering")

    def test_the_tv_moved_on_and_the_pc_is_still_on_the_old_video_so_the_pc_follows(self):
        action, why = g.resync_plan(tv(), "other", pc("abc"), "abc")
        self.assertEqual(action, "follow")
        self.assertIn("PC followed", why)

    def test_the_pc_is_on_a_newer_video_the_tv_never_got_so_the_tv_is_sent_it(self):
        self.assertEqual(g.resync_plan(tv(), "old", pc("new"), "old2")[0], "recast")

    def test_a_tv_that_left_playlet_or_stopped_is_sent_the_pcs_video_again(self):
        for state in (None, tv(app="Netflix"), tv("none", app="")):
            self.assertEqual(g.resync_plan(state, "", pc(), "abc")[0], "recast", state)

    def test_an_ad_on_the_pc_waits(self):
        action, why = g.resync_plan(tv(), "abc", pc(ad=True), "abc")
        self.assertEqual(action, "ad")
        self.assertIn("ad", why)

    def test_nothing_in_the_youtube_tab_is_said_plainly(self):
        action, why = g.resync_plan(tv(), "abc", None, "abc")
        self.assertEqual(action, "nothing")
        self.assertIn("YouTube tab", why)


class FakeBridge:
    """The page's player: moves when told to, optionally landing late like a real YouTube seek."""

    def __init__(self, t, paused=False, lag=()):
        self.t, self.paused, self.sent, self.lag, self.ad = t, paused, [], list(lag), False

    def player(self):
        return {"id": "abc", "t": self.t, "paused": self.paused, "ad": self.ad}

    def send(self, cmd):
        self.sent.append(cmd)
        if "sync" in cmd:
            self.t = cmd["sync"] - (self.lag.pop(0) if self.lag else 0.0)
            if "syncPaused" in cmd:
                self.paused = cmd["syncPaused"]


class Sync(unittest.TestCase):
    def sync(self, bridge, current="abc"):
        with mock.patch.object(g.threading, "Thread"):
            s = g.CastSync(lambda: "h", bridge, None)
        s.current, s.title = current, "A video"
        patcher = mock.patch.object(g.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        return s

    def run_resync(self, s, state, tv_pos=None, vid="abc", cast=None):
        with mock.patch.object(g.Remote, "state", return_value=state), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": vid, "title": "T"}), \
                mock.patch.object(s, "_tv_position", return_value=tv_pos), \
                mock.patch.object(g.Remote, "cast") as cast_call:
            result = s._resync("h")
            self.last_cast = cast_call
        return result

    # ---- aligning ---------------------------------------------------------------
    def test_a_pc_player_that_drifted_is_moved_onto_the_tv_and_checked_again(self):
        b = FakeBridge(92.0, lag=[1.2])                 # the first seek lands 1.2 s late, so a second pass is needed
        s = self.sync(b)
        ok, text = self.run_resync(s, tv(pos=100.0), tv_pos=100.0)
        self.assertTrue(ok)
        self.assertEqual(len(b.sent), 2)
        self.assertIn("8.0 s behind", text)
        self.assertEqual(b.sent[0]["syncId"], "abc")
        self.assertAlmostEqual(b.sent[0]["sync"], 100.3)          # a little ahead: the page applies it on its next poll

    def test_two_players_already_in_step_are_left_alone(self):
        b = FakeBridge(100.2)
        s = self.sync(b)
        ok, text = self.run_resync(s, tv(pos=100.0), tv_pos=100.0)
        self.assertEqual((ok, text), (True, "Already in step"))
        self.assertEqual(b.sent, [])

    def test_a_correction_the_page_never_makes_is_reported_not_called_success(self):
        class Stuck(FakeBridge):
            def send(self, cmd):
                self.sent.append(cmd)                  # the page ignores it (an ad started, say)
        b = Stuck(50.0)
        s = self.sync(b)
        ok, text = self.run_resync(s, tv(pos=100.0), tv_pos=100.0)
        self.assertFalse(ok)
        self.assertEqual(len(b.sent), g.CastSync.RESYNC_ROUNDS)
        self.assertIn("still about", text)

    def test_a_paused_tv_pauses_the_pc_player_at_exactly_its_position(self):
        b = FakeBridge(80.0, paused=False)
        s = self.sync(b)
        ok, _text = self.run_resync(s, tv("pause", pos=100.0))
        self.assertTrue(ok)
        self.assertEqual(b.sent[0]["syncPaused"], True)
        self.assertEqual(b.sent[0]["sync"], 100.0)          # nothing moves on a paused TV, so no head start
        self.assertTrue(b.paused)

    def test_a_playing_tv_starts_a_paused_pc_player(self):
        b = FakeBridge(100.0, paused=True)
        s = self.sync(b)
        ok, _text = self.run_resync(s, tv(pos=100.0), tv_pos=100.0)
        self.assertTrue(ok)
        self.assertEqual(b.sent[0]["syncPaused"], False)
        self.assertFalse(b.paused)

    def test_a_buffering_tv_is_given_time_then_measured(self):
        b = FakeBridge(90.0)
        s = self.sync(b)
        states = [tv("buffer"), tv("buffer"), tv("play", 100.0)]
        with mock.patch.object(g.Remote, "state", side_effect=states + [tv("play", 100.0)] * 5), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "abc"}), \
                mock.patch.object(s, "_tv_position", return_value=100.0):
            ok, _text = s._resync("h")
        self.assertTrue(ok)
        self.assertTrue(b.sent)

    def test_a_tv_that_keeps_buffering_is_reported(self):
        b = FakeBridge(90.0)
        s = self.sync(b)
        clock = iter(range(0, 1000))
        with mock.patch.object(g.Remote, "state", return_value=tv("buffer")), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "abc"}), \
                mock.patch.object(g.time, "time", side_effect=lambda: next(clock) * 0.5):
            ok, text = s._resync("h")
        self.assertFalse(ok)
        self.assertIn("buffering", text)
        self.assertEqual(b.sent, [])

    def test_an_ad_changes_nothing(self):
        b = FakeBridge(90.0)
        b.ad = True
        s = self.sync(b)
        ok, text = self.run_resync(s, tv(pos=100.0), tv_pos=100.0)
        self.assertFalse(ok)
        self.assertIn("ad", text)
        self.assertEqual(b.sent, [])

    # ---- the TV and the PC disagree about the video -------------------------------------
    def test_the_pc_follows_a_tv_that_moved_on(self):
        b = FakeBridge(10.0)
        s = self.sync(b, current="abc")
        ok, text = self.run_resync(s, tv(pos=200.0), vid="next")
        self.assertTrue(ok)
        self.assertEqual(s.current, "next")                    # adopted, so the page's own load is not repeated
        nav = b.sent[0]["navigate"]
        self.assertIn("v=next", nav)
        self.assertIn("t=204s", nav)                            # a few seconds past the TV: the page takes that to start
        self.assertIn("followed", text)
        self.last_cast.assert_not_called()

    def test_the_tv_is_sent_the_pcs_video_when_it_lost_it(self):
        b = FakeBridge(50.0)
        s = self.sync(b, current="old")
        with mock.patch.object(s, "_started", return_value=True) as started:
            ok, text = self.run_resync(s, tv("none", app=""), vid="")
        self.assertTrue(ok)
        args = self.last_cast.call_args[0]
        self.assertEqual(args[1], "abc")
        self.assertAlmostEqual(args[2], 50.0 + s.lead)         # where the PC will be once the TV is playing
        self.assertEqual(s.current, "abc")
        self.assertIn("sent to it again", text)

    def test_a_paused_pc_video_is_sent_to_the_tv_and_paused_there(self):
        b = FakeBridge(50.0, paused=True)
        s = self.sync(b, current="old")
        with mock.patch.object(s, "_ensure") as ensure:
            self.run_resync(s, tv("none", app=""), vid="")
        self.assertEqual(self.last_cast.call_args[0][2], 50.0)
        ensure.assert_called_once_with("h", "pause")

    # ---- running it ------------------------------------------------------------------------------------
    def test_asking_twice_while_it_runs_queues_only_one(self):
        s = self.sync(FakeBridge(1.0))
        self.assertTrue(s.request_resync())
        self.assertFalse(s.request_resync())
        self.assertEqual(s.q.get_nowait(), {"kind": "resync"})
        self.assertTrue(s.q.empty())

    def test_the_result_is_kept_for_the_status_text_and_the_button_is_free_again(self):
        s = self.sync(FakeBridge(100.0))
        s.syncing = True
        with mock.patch.object(s, "_resync", return_value=(True, "Already in step")):
            s._handle({"kind": "resync"})
        self.assertEqual((s.note, s.note_ok, s.syncing), ("Already in step", True, False))
        self.assertGreater(s.note_until, time.time())

    def test_a_tv_that_cannot_be_reached_is_reported_and_does_not_leave_it_stuck(self):
        s = self.sync(FakeBridge(100.0))
        s.syncing = True
        with mock.patch.object(s, "_resync", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                s._handle({"kind": "resync"})
        self.assertFalse(s.syncing)
        self.assertFalse(s.note_ok)

    # ---- the echo of our own nudge ------------------------------------------------------------------------
    def test_the_page_reporting_the_move_we_just_made_is_not_sent_back_to_the_tv(self):
        s = self.sync(FakeBridge(100.0))
        s._move_pc(100.3)
        with mock.patch.object(g.Remote, "cast") as cast_call:
            s._handle({"kind": "seek", "id": "abc", "t": 100.9, "paused": False})
        cast_call.assert_not_called()

    def test_a_real_seek_by_the_user_near_that_time_still_counts_after_the_echo_window(self):
        s = self.sync(FakeBridge(100.0))
        s._move_pc(100.3)
        s._nudge = (s._nudge[0], s._nudge[1], time.time() - g.CastSync.ECHO - 1)
        with mock.patch.object(g.Remote, "cast") as cast_call, \
                mock.patch.object(g.Remote, "state", return_value=None), \
                mock.patch.object(s, "_started", return_value=False):
            s._handle({"kind": "seek", "id": "abc", "t": 100.9, "paused": False})
        cast_call.assert_called_once()

    def test_a_seek_far_from_the_nudge_is_the_user_and_is_sent(self):
        s = self.sync(FakeBridge(100.0))
        s._move_pc(100.3)
        with mock.patch.object(g.Remote, "cast") as cast_call, mock.patch.object(s, "_started", return_value=False):
            s._handle({"kind": "seek", "id": "abc", "t": 300.0, "paused": False})
        cast_call.assert_called_once()

    # ---- the automatic check no longer sleeps for 20 s when the TV could not be measured ----------------------
    def test_a_tv_that_could_not_be_measured_is_looked_at_again_within_a_second(self):
        s = self.sync(FakeBridge(100.0))
        with mock.patch.object(s, "_tv_position", return_value=None):
            s._align("h")
        due_in = s._check_at - time.time()
        self.assertTrue(0.5 < due_in <= 1.0, due_in)

    def test_a_paused_tv_costs_one_cheap_look_a_second_never_precise_polling(self):
        s = self.sync(FakeBridge(100.0, paused=True))
        paused = {"state": "pause", "app": "Playlet", "pos": 100.0, "dur": 900}
        with mock.patch.object(g.Remote, "state", return_value=paused) as look, \
                mock.patch.object(s, "_tv_position") as precise:
            for _ in range(8):
                s._watch_at = 0.0
                s._watch("h")
        precise.assert_not_called()
        self.assertEqual(look.call_count, 8)


class Extension(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "extension", "feed.js"), encoding="utf-8") as f:
            self.src = f.read()

    def test_it_tells_the_app_about_ads_and_does_not_move_the_player_during_one(self):
        self.assertIn("ad: isAd() ? 1 : 0", self.src)
        self.assertIn("cmd.sync != null && cmd.syncId === castId && v && !isAd()", self.src)

    def test_it_can_pause_and_play_the_player_for_a_resync_without_echoing_it(self):
        self.assertIn("cmd.syncPaused === true", self.src)
        self.assertIn("cmd.syncPaused === false", self.src)
        self.assertIn("Date.now() < ignoreSeeksUntil", self.src)

    def test_the_bridge_carries_the_ad_flag(self):
        b = Bridge()
        b._note_player({"id": ["abc"], "t": ["5.0"], "paused": ["0"], "ad": ["1"]})
        self.assertTrue(b.player()["ad"])
        b._note_player({"id": ["abc"], "t": ["5.0"], "paused": ["0"]})
        self.assertFalse(b.player()["ad"])


class Phone(unittest.TestCase):
    def backend(self, now_source="youtube", autocast=True, connected=True, started=True):
        calls = []
        app = SimpleNamespace(last_tv={}, autocast=autocast, bridge=SimpleNamespace(connected=lambda: connected),
                              sync=SimpleNamespace(request_resync=lambda: calls.append(1) or started))
        b = g.PhoneBackend(app)
        b.now = lambda tv: {"source": now_source} if now_source else None
        return b, calls

    def test_it_starts_a_resync_for_a_youtube_video(self):
        b, calls = self.backend()
        self.assertIn("Resyncing", b.resync())
        self.assertEqual(calls, [1])

    def test_it_refuses_clearly_when_it_cannot_help(self):
        for kwargs, word in (({"now_source": None}, "YouTube"), ({"now_source": "web"}, "YouTube"),
                             ({"autocast": False}, "Auto-cast"), ({"connected": False}, "linked")):
            b, calls = self.backend(**kwargs)
            with self.assertRaises(ValueError) as cm:
                b.resync()
            self.assertIn(word, str(cm.exception))
            self.assertEqual(calls, [])

    def test_asking_again_while_running_says_so(self):
        b, _ = self.backend(started=False)
        self.assertEqual(b.resync(), "Already resyncing")

    def test_the_route_and_the_page_button(self):
        class B:
            def resync(self):
                return "Resyncing the PC with the TV…"
        srv = pr.PhoneRemote(B(), "tok", b"", port=0, bind="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/resync", data=b"{}",
                                     headers={"X-Token": "tok", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"message": "Resyncing the PC with the TV…"})
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/?k=tok", timeout=5) as r:
            page = r.read().decode()
        for needle in ('id="resyncbtn"', "/api/resync", 'n.source==="youtube"'):
            self.assertIn(needle, page)

    def test_the_phone_shows_what_resync_just_did(self):
        app = SimpleNamespace(
            sync=SimpleNamespace(note="In step again", note_until=time.time() + 5, note_ok=True),
            web=SimpleNamespace(phone_note=lambda: None, phone_watching=lambda: None, sync=None),
            app=None, listener=None, pc_volume=100, tvvol=SimpleNamespace(level=10, muted=False), last_tv=None)
        b = g.PhoneBackend(app)
        self.assertEqual(b.state()["note"], {"text": "In step again", "ok": True})


class RememberedWindow(unittest.TestCase):
    SCREEN = (0, 0, 1920, 1080)

    def test_a_window_on_screen_comes_back_where_it_was(self):
        saved = {"w": 1300, "h": 700, "x": 200, "y": 100, "zoomed": True}
        self.assertEqual(g.restore_geometry(saved, self.SCREEN), (1300, 700, 200, 100, True))

    def test_a_window_saved_on_a_monitor_that_is_gone_opens_centred_instead(self):
        saved = {"w": 1100, "h": 700, "x": 2500, "y": 100}
        self.assertIsNone(g.restore_geometry(saved, self.SCREEN))
        self.assertIsNone(g.restore_geometry({"w": 1100, "h": 700, "x": 200, "y": 4000}, self.SCREEN))

    def test_a_monitor_to_the_left_with_negative_coordinates_is_fine_when_it_exists(self):
        saved = {"w": 1250, "h": 700, "x": -1500, "y": 50}
        self.assertEqual(g.restore_geometry(saved, (-1920, 0, 3840, 1080))[:4], (1250, 700, -1500, 50))
        self.assertIsNone(g.restore_geometry(saved, self.SCREEN))

    def test_a_size_below_the_minimum_or_above_the_screen_is_clamped(self):
        w, h, *_ = g.restore_geometry({"w": 100, "h": 100, "x": 10, "y": 10}, self.SCREEN)
        self.assertEqual((w, h), (g.MIN_W, g.MIN_H))
        w, h, *_ = g.restore_geometry({"w": 9000, "h": 9000, "x": 0, "y": 0}, self.SCREEN)
        self.assertEqual((w, h), (1920, 1080))

    def test_nothing_or_junk_saved_means_the_usual_centring(self):
        for junk in (None, {}, {"w": "x"}, "text", {"w": 1, "h": 1, "x": 1}):
            self.assertIsNone(g.restore_geometry(junk, self.SCREEN), junk)


class FindRoku(unittest.TestCase):
    REPLY = (b"HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nST: roku:ecp\r\nUSN: uuid:roku:ecp:X0000\r\n"
             b"Ext: \r\nServer: Roku/9.4.0 UPnP/1.0\r\nLOCATION: http://10.0.0.77:8060/\r\n\r\n")

    def test_a_rokus_answer_gives_its_address(self):
        self.assertEqual(roku_find.parse_reply(self.REPLY), "10.0.0.77")

    def test_other_devices_and_junk_are_ignored(self):
        other = self.REPLY.replace(b"roku:ecp", b"upnp:rootdevice")
        for data in (other, b"", b"\xff\xfe", b"HTTP/1.1 200 OK\r\nST: roku:ecp\r\n\r\n",
                     self.REPLY.replace(b"10.0.0.77", b"999.0.0.1")):
            self.assertIsNone(roku_find.parse_reply(data), data)

    def test_the_search_collects_each_answering_roku_once(self):
        answers = [(self.REPLY, ("10.0.0.77", 1900)), (self.REPLY, ("10.0.0.77", 1900)),
                   (self.REPLY.replace(b"10.0.0.77", b"10.0.0.78"), ("10.0.0.78", 1900)), (b"junk", ("1.1.1.1", 1))]
        sock = mock.MagicMock()
        sock.recvfrom.side_effect = answers + [OSError("closed")]
        with mock.patch.object(roku_find.socket, "socket", return_value=sock):
            self.assertEqual(roku_find.search(timeout=5), ["10.0.0.77", "10.0.0.78"])
        sock.close.assert_called_once()

    def test_no_network_means_nobody_found_not_a_crash(self):
        with mock.patch.object(roku_find.socket, "socket", side_effect=OSError("no network")):
            self.assertEqual(roku_find.search(timeout=1), [])


class Widgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_a_chip_behaves_like_the_label_it_replaces(self):
        c = ui_kit.Chip(self.root, "Discord: Off", lambda: None)
        self.assertFalse(c.on)
        c.config(text="Discord: On", fg=ui_kit.ACCENT)
        self.assertTrue(c.on)
        self.assertEqual(c.text, "Discord: On")
        c.config(fg=ui_kit.MUTED)
        self.assertFalse(c.on)
        c.config(text="x", font=("Arial", 9))                     # a font argument is accepted and ignored
        self.assertEqual(c.text, "x")

    def test_a_chip_is_as_wide_as_its_text(self):
        c = ui_kit.Chip(self.root, "a", lambda: None)
        narrow = int(c["width"])
        c.config(text="Syncing the player…")
        self.assertGreater(int(c["width"]), narrow)

    def test_a_chip_repaints_for_hover_and_press_without_error(self):
        c = ui_kit.Chip(self.root, "Go", lambda: None, solid=True)
        for kw in ({"hot": True}, {"down": True}, {"hot": False, "down": False}):
            c._set(**kw)
        self.assertEqual(c.type(c.find_all()[0]), "image")

    def test_text_is_shortened_to_the_room_it_has(self):
        font = (ui_kit.FONT, 9)
        full = "On TV  ·  A very long video title that would run into the buttons"
        fitted = ui_kit.fit_text(font, full, 120)
        self.assertTrue(fitted.endswith("…"))
        self.assertLessEqual(tk.font.Font(font=font).measure(fitted), 120)
        self.assertEqual(ui_kit.fit_text(font, "Short", 120), "Short")
        self.assertEqual(ui_kit.fit_text(font, full, 3), "")

    def test_rounded_shapes_are_pictures_made_once_per_size_and_colour(self):
        canvas = tk.Canvas(self.root)
        a = ui_kit.rounded(canvas, 0, 0, 40, 20, 6, fill="#ff0000")
        b = ui_kit.rounded(canvas, 5, 5, 45, 25, 6, fill="#ff0000")
        self.assertEqual(canvas.type(a), "image")
        self.assertEqual(canvas.itemcget(a, "image"), canvas.itemcget(b, "image"))      # the same cached picture
        c = ui_kit.rounded(canvas, 0, 0, 40, 20, 6, fill="#00ff00")
        self.assertNotEqual(canvas.itemcget(a, "image"), canvas.itemcget(c, "image"))

    def test_a_progress_bar_only_seeks_when_it_can(self):
        got = []
        bar = ui_kit.ProgressBar(self.root, got.append, width=200)
        click = SimpleNamespace(x=50)
        bar._click(click)
        self.assertEqual(got, [])
        bar.set(0.2, True)
        bar._click(click)
        self.assertAlmostEqual(got[0], 0.25)

    def test_the_status_text_never_runs_into_the_nav_buttons(self):
        class Stub(tk.Toplevel):
            """Just enough of the App to build and lay out its top bar (same Tk as the other widgets)."""
            _build_topbar, _layout_bar, _set_sync_status = g.App._build_topbar, g.App._layout_bar, g.App._set_sync_status
            autocast, discord = True, SimpleNamespace(enabled={})
            bridge = SimpleNamespace(send=lambda c: None)
            open_phone = open_health = toggle_typebox = _show = _set_autocast = _set_discord_youtube = _resync = lambda self, *a: None

        app = Stub(self.root)
        app.withdraw()
        self.addCleanup(app.destroy)
        g.App._build_topbar(app)
        app.bar.winfo_width = lambda: g.MIN_W                       # an unmapped canvas would say 1
        app._set_sync_status("On TV  ·  " + "very long title " * 12, g.GREEN)
        left, _top, right, _bottom = app.bar.bbox(app.r_status)
        self.assertGreaterEqual(left, app._bar_left)
        self.assertGreater(right, left)
        self.assertTrue(app.bar.itemcget(app.r_status, "text").endswith("…"))
        self.assertGreater(left, 0)
        app._set_sync_status("Ready", g.GREEN)                      # a short one is left whole
        self.assertEqual(app.bar.itemcget(app.r_status, "text"), "Ready")


class TypeBoxIcon(unittest.TestCase):
    """The keyboard icon in the top bar shows and hides the Type on TV box."""

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def app(self, page="Listen"):
        class Stub(tk.Toplevel):
            toggle_typebox, _tv_keyboard, _stray_key = g.App.toggle_typebox, g.App._tv_keyboard, g.App._stray_key
            _focused = lambda self: None          # noqa: E731
            _pop_up = lambda self: None           # noqa: E731

        a = Stub(self.root)
        a.withdraw()
        self.addCleanup(a.destroy)
        a.page, a.shown, a._kb_from, a.keys_sent = page, [], None, []
        a._show = lambda name: (a.shown.append(name), setattr(a, "page", name))
        a.type_wrap, a.vol_row = tk.Frame(a), tk.Frame(a)
        a.vol_row.pack()
        a.kb_btn = ui_kit.NavButton(a, "keyboard", lambda: None)
        a._type_visible = a._type_auto = False
        a.typebox = SimpleNamespace(tv=None, focused=0, render=lambda: None,
                                    focus_set=lambda: setattr(a.typebox, "focused", a.typebox.focused + 1),
                                    set_tv=lambda st: setattr(a.typebox, "tv", st), wants=lambda e: True,
                                    _key=lambda e: a.keys_sent.append(e) or "break")
        return a

    def test_the_icon_shows_the_box_lights_up_and_hides_it_again(self):
        a = self.app()
        a.toggle_typebox()
        self.assertEqual(a.type_wrap.winfo_manager(), "pack")
        self.assertTrue(a.kb_btn.active)
        self.assertEqual(a.typebox.focused, 1)                  # the cursor is already in it
        a.toggle_typebox()
        self.assertEqual(a.type_wrap.winfo_manager(), "")
        self.assertFalse(a.kb_btn.active)

    def test_the_box_sits_above_the_volume_row_where_it_used_to_be(self):
        a = self.app()
        a.toggle_typebox()
        order = a.pack_slaves()
        self.assertLess(order.index(a.type_wrap), order.index(a.vol_row))

    def test_turning_it_on_from_another_tab_brings_up_the_listen_tab(self):
        a = self.app(page="YouTube")
        a.toggle_typebox()
        self.assertEqual(a.shown, ["Listen"])

    def test_keys_typed_on_the_listen_tab_only_go_to_the_tv_while_the_box_is_showing(self):
        a = self.app()
        e = SimpleNamespace(widget=a)
        self.assertIsNone(a._stray_key(e))
        self.assertEqual(a.keys_sent, [])
        a.toggle_typebox()
        a.typebox.focused = 0
        self.assertEqual(a._stray_key(e), "break")
        self.assertEqual(len(a.keys_sent), 1)

    def test_the_tv_asking_for_text_shows_the_box_and_hides_it_again_when_it_closes(self):
        a = self.app(page="Web")
        a._tv_keyboard({"text": "", "masked": False})
        self.assertTrue(a._type_visible)
        self.assertEqual(a.type_wrap.winfo_manager(), "pack")
        a._tv_keyboard(None)
        self.assertFalse(a._type_visible)
        self.assertEqual(a.type_wrap.winfo_manager(), "")
        self.assertIn("Web", a.shown)                           # and back to the tab the user was on

    def test_a_box_the_user_opened_themselves_stays_after_the_tv_keyboard_closes(self):
        a = self.app()
        a.toggle_typebox()
        a._tv_keyboard({"text": "", "masked": False})
        a._tv_keyboard(None)
        self.assertTrue(a._type_visible)


if __name__ == "__main__":
    unittest.main()
