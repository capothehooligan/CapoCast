"""The YouTube tab's keyboard focus, and "Also watch on the PC" from the phone."""
import json
import os
import sys
import unittest
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phone_remote as pr  # noqa: E402
import roku_listen_gui as g  # noqa: E402
import yt_embed  # noqa: E402


class FakeUser32:
    """Just the focus calls ChromeEmbed makes."""

    def __init__(self, focus=0, children=()):
        self.focus, self.children, self.set_to, self.windows = focus, set(children), [], {10}

    def IsWindow(self, h):
        return h in self.windows

    def GetFocus(self):
        return self.focus

    def IsChild(self, parent, h):
        return (parent, h) in self.children

    def SetFocus(self, h):
        self.set_to.append(h)
        self.focus = h


class ChromeFocus(unittest.TestCase):
    def embed(self, **kw):
        fake = FakeUser32(**kw)
        patcher = mock.patch.object(yt_embed, "user32", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        c = yt_embed.ChromeEmbed()
        c.hwnd = 10
        return c, fake

    def test_focus_in_the_app_s_own_window_is_moved_into_chrome(self):
        c, u = self.embed(focus=99)
        self.assertFalse(c.has_focus())
        self.assertTrue(c.keep_focus())
        self.assertEqual(u.set_to, [10])
        self.assertTrue(c.has_focus())

    def test_focus_already_in_chrome_or_in_one_of_its_inner_windows_is_left_alone(self):
        for focus, kids in ((10, ()), (55, ((10, 55),))):
            c, u = self.embed(focus=focus, children=kids)
            self.assertTrue(c.has_focus(), focus)
            self.assertFalse(c.keep_focus())
            self.assertEqual(u.set_to, [])

    def test_no_window_means_nothing_to_focus(self):
        c, u = self.embed(focus=99)
        c.hwnd = None
        self.assertFalse(c.has_focus())
        self.assertFalse(c.keep_focus())
        self.assertEqual(u.set_to, [])

    def test_the_launch_url_is_optional(self):
        popen = mock.patch.object(yt_embed.subprocess, "Popen").start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(yt_embed, "find_chrome", return_value="chrome.exe"), \
                mock.patch.object(yt_embed, "chrome_windows", return_value={}):
            yt_embed.ChromeEmbed().launch()
            yt_embed.ChromeEmbed().launch("https://www.youtube.com/watch?v=abc&t=5s")
        self.assertEqual([c[0][0][-1] for c in popen.call_args_list],
                         ["--app=https://www.youtube.com/", "--app=https://www.youtube.com/watch?v=abc&t=5s"])


class FocusTick(unittest.TestCase):
    """App._yt_focus_tick and the hand-back when the YouTube tab is left, without building the window."""

    def app(self, page="YouTube", front=True, alive=True, focused=False, steals=False):
        chrome = SimpleNamespace(alive=lambda: alive, has_focus=lambda: focused, moved=0)

        def keep():
            if focused:
                return False
            chrome.moved += 1
            if not steals:
                chrome.has_focus = lambda: True
            return True
        chrome.keep_focus = keep
        app = SimpleNamespace(page=page, chrome=chrome, FOCUS_EVERY=80, scheduled=[], _yt_focus_tick=None,
                              _app_in_front=lambda: front, after=lambda ms, fn: app.scheduled.append(ms))
        return app

    def tick(self, app):
        g.App._yt_focus_tick(app)

    def test_the_keyboard_is_moved_into_chrome_while_the_tab_is_showing(self):
        a = self.app()
        self.tick(a)
        self.assertEqual(a.chrome.moved, 1)
        self.assertEqual(a.scheduled, [80])                   # and it keeps checking

    def test_nothing_happens_on_other_tabs_behind_other_apps_or_without_chrome(self):
        for kw in ({"page": "Listen"}, {"front": False}, {"alive": False}):
            a = self.app(**kw)
            self.tick(a)
            self.assertEqual(a.chrome.moved, 0, kw)
            self.assertEqual(a.scheduled, [80], kw)           # the loop never stops

    def test_a_chrome_that_refuses_is_not_fought_every_tick(self):
        a = self.app(steals=True)
        with mock.patch.object(g, "log"), mock.patch.object(g.time, "time", return_value=1000.0):
            self.tick(a)
            self.tick(a)
            self.tick(a)
        self.assertEqual(a.chrome.moved, 1)
        with mock.patch.object(g, "log"), mock.patch.object(g.time, "time", return_value=1001.5):
            self.tick(a)
        self.assertEqual(a.chrome.moved, 2)                   # tries again a second later

    def test_leaving_the_tab_gives_the_keyboard_back_to_the_app(self):
        for had, expect in ((True, [("show", False), "focus_tk"]), (False, [("show", False)])):
            calls = []
            chrome = SimpleNamespace(has_focus=lambda: had, show=lambda v: calls.append(("show", v)))
            app = SimpleNamespace(chrome=chrome, _focus_tk=lambda: calls.append("focus_tk"))
            g.App._hide_chrome(app)
            self.assertEqual(calls, expect, had)


class WatchOnPhone(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.watching = None
        web = SimpleNamespace(phone_watching=lambda: self.watching, sync=None,
                              guard=SimpleNamespace(snapshot=lambda: {"title": "Page title"}))
        self.app = SimpleNamespace(
            web=web, _host="10.0.0.5", last_tv=None, autocast=True,
            chrome=SimpleNamespace(alive=lambda: True), bridge=SimpleNamespace(connected=lambda: True),
            after=lambda ms, fn: self.calls.append(("after", fn)),
            show_web_on_pc=lambda: None,
            watch_youtube_on_pc=lambda *a: self.calls.append(("youtube", *a)))
        self.b = g.PhoneBackend(self.app)

    def tv(self, **kw):
        return {"state": "play", "app": "Playlet", "pos": 100.0, "dur": 600.0, **kw}

    def run_after(self):
        for kind, fn in [c for c in self.calls if c[0] == "after"]:
            fn()

    def playlet(self, **now):
        return mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "abc123", "title": "A video", **now})

    # ---- what the phone is told
    def test_youtube_and_web_casts_can_be_opened_on_the_pc_but_other_apps_cannot(self):
        with self.playlet():
            self.assertTrue(self.b.now(self.tv())["pc"])
        self.watching = {"id": 1.0, "title": "T", "url": "u"}
        self.app.web.sync = SimpleNamespace(offset=0.0, dur=100.0, live=False)
        self.assertTrue(self.b.now(self.tv(app="RokuCast"))["pc"])
        self.watching = None
        self.assertFalse(self.b.now(self.tv(app="Netflix"))["pc"])

    # ---- YouTube
    def test_a_youtube_video_opens_on_the_pc_a_little_past_where_the_tv_is(self):
        self.app.last_tv = self.tv()
        with self.playlet():
            msg = self.b.watch_on_pc()
        self.run_after()
        self.assertEqual(self.calls[-1], ("youtube", "abc123", "A video", 104.0))
        self.assertIn("in step with the TV", msg)

    def test_the_position_stays_inside_the_video(self):
        self.app.last_tv = self.tv(pos=598.0)
        with self.playlet():
            self.b.watch_on_pc()
        self.run_after()
        self.assertEqual(self.calls[-1][3], 595.0)

    def test_with_auto_cast_off_the_message_says_it_will_not_stay_in_step(self):
        self.app.autocast = False
        self.app.last_tv = self.tv()
        with self.playlet():
            self.assertIn("won't stay in step", self.b.watch_on_pc())

    def test_an_unknown_video_is_reported(self):
        self.app.last_tv = self.tv()
        with mock.patch.object(g.Remote, "playlet_now", return_value={}), \
                mock.patch.object(g.discord_presence, "youtube_info", return_value=("", "")):
            with self.assertRaises(ValueError):
                self.b.watch_on_pc()
        self.assertEqual(self.calls, [])

    def test_a_youtube_tab_the_extension_is_not_linked_to_is_reported_not_queued(self):
        self.app.last_tv = self.tv()
        self.app.bridge = SimpleNamespace(connected=lambda: False)
        with self.playlet(), self.assertRaises(ValueError) as cm:
            self.b.watch_on_pc()
        self.assertIn("extension", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_a_youtube_tab_that_is_not_open_yet_is_simply_opened(self):
        self.app.last_tv = self.tv()
        self.app.chrome = SimpleNamespace(alive=lambda: False)
        self.app.bridge = SimpleNamespace(connected=lambda: False)
        with self.playlet():
            self.b.watch_on_pc()
        self.run_after()
        self.assertEqual(self.calls[-1][0], "youtube")

    # ---- Web, and the rest
    def test_a_web_cast_brings_up_the_web_tab(self):
        self.watching = {"id": 1.0, "title": "T", "url": "u"}
        self.app.web.sync = SimpleNamespace(offset=0.0, dur=100.0, live=False)
        shown = []
        self.app.show_web_on_pc = lambda: shown.append(True)
        self.app.last_tv = self.tv(app="RokuCast")
        self.assertIn("Web tab", self.b.watch_on_pc())
        self.run_after()
        self.assertEqual(shown, [True])

    def test_nothing_playing_and_other_apps_are_refused_with_a_reason(self):
        with self.assertRaises(ValueError):
            self.b.watch_on_pc()
        self.app.last_tv = self.tv(app="Netflix")
        with self.assertRaises(ValueError) as cm:
            self.b.watch_on_pc()
        self.assertIn("Netflix", str(cm.exception))


class WatchOnPc(unittest.TestCase):
    """App.watch_youtube_on_pc / show_web_on_pc, without building the window."""

    def app(self, alive=True, waiting=False):
        sent, shown = [], []
        sync = SimpleNamespace(adopted=[], adopt=lambda v, t: sync.adopted.append((v, t)))
        chrome = SimpleNamespace(alive=lambda: alive, waiting=lambda: waiting)
        app = SimpleNamespace(sync=sync, chrome=chrome, bridge=SimpleNamespace(send=sent.append), sent=sent, shown=shown,
                              reopened=[], _show=shown.append, _pop_up=lambda: shown.append("pop"))
        app._yt_reopen = app.reopened.append
        return app

    def test_an_open_youtube_tab_is_sent_to_the_video_through_the_extension(self):
        a = self.app()
        g.App.watch_youtube_on_pc(a, "abc123", "A video", 104.9)
        self.assertEqual(a.sent, [{"navigate": "https://www.youtube.com/watch?v=abc123&t=104s"}])
        self.assertEqual(a.sync.adopted, [("abc123", "A video")])      # so the page's "load" doesn't reload the TV
        self.assertEqual(a.shown, ["YouTube", "pop"])
        self.assertEqual(a.reopened, [])

    def test_a_closed_youtube_tab_is_opened_straight_on_the_video(self):
        a = self.app(alive=False)
        g.App.watch_youtube_on_pc(a, "abc123", "A video", 5)
        self.assertEqual(a.reopened, ["https://www.youtube.com/watch?v=abc123&t=5s"])
        self.assertEqual(a.sent, [])

    def test_a_tab_that_is_still_opening_gets_the_address_on_its_first_poll(self):
        a = self.app(alive=False, waiting=True)
        g.App.watch_youtube_on_pc(a, "abc123", "A video", 5)
        self.assertEqual(len(a.sent), 1)
        self.assertEqual(a.reopened, [])

    def test_the_video_id_cannot_break_out_of_the_address(self):
        a = self.app()
        g.App.watch_youtube_on_pc(a, "x&list=evil#", "T", 0)
        self.assertEqual(a.sent[0]["navigate"], "https://www.youtube.com/watch?v=x%26list%3Devil%23&t=0s")

    def test_the_web_tab_is_brought_up(self):
        a = self.app()
        g.App.show_web_on_pc(a)
        self.assertEqual(a.shown, ["Web", "pop"])


class Adopt(unittest.TestCase):
    def sync(self):
        with mock.patch.object(g.threading, "Thread"):
            return g.CastSync(lambda: "h", SimpleNamespace(), None)

    def test_a_video_started_from_the_phone_is_not_reloaded_when_the_pc_opens_it(self):
        s = self.sync()
        s.adopt("abc", "A video")
        st = {"app": "Playlet", "state": "play", "pos": 110.0}
        with mock.patch.object(g.Remote, "state", return_value=st), \
                mock.patch.object(g.Remote, "playlet_now", return_value={"videoid": "abc"}):
            self.assertTrue(s._already_playing("h", "abc", 104))
            self.assertFalse(s._already_playing("h", "abc", 10))      # a different point is still a real seek
            self.assertFalse(s._already_playing("h", "other", 104))
        self.assertEqual((s.current, s.title), ("abc", "A video"))

    def test_adopting_nothing_changes_nothing(self):
        s = self.sync()
        s.adopt("", "x")
        s.adopt(None)
        self.assertIsNone(s.current)


class Route(unittest.TestCase):
    def test_the_phone_route_reaches_the_backend_and_needs_the_token(self):
        class B:
            def watch_on_pc(self):
                return "Opening it on the PC"
        srv = pr.PhoneRemote(B(), "secret-token", b"", port=0, bind="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        with mock.patch("phone_remote.time.sleep"):
            def call(token):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/watchpc", data=b"{}",
                                             headers={"X-Token": token, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, None
            self.assertEqual(call("secret-token"), (200, {"message": "Opening it on the PC"}))
            self.assertEqual(call("wrong")[0], 401)

    def test_a_refusal_comes_back_as_a_message_for_the_phone(self):
        class B:
            def watch_on_pc(self):
                raise ValueError("Netflix can't be opened on the PC.")
        srv = pr.PhoneRemote(B(), "secret-token", b"", port=0, bind="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/watchpc", data=b"{}",
                                     headers={"X-Token": "secret-token", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("Netflix", json.loads(cm.exception.read())["error"])

    def test_the_page_has_the_button_and_shows_it_only_for_casts_that_can_open_on_the_pc(self):
        srv = pr.PhoneRemote(object(), "secret-token", b"", port=0, bind="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/?k=secret-token", timeout=5) as r:
            page = r.read().decode()
        for needle in ('id="onpc"', "/api/watchpc", "n.pc?"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
