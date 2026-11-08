"""The phone remote: link parsing, the token, what the phone may press, and the page itself."""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phone_remote as pr  # noqa: E402


class YoutubeLinks(unittest.TestCase):
    def test_the_usual_shapes(self):
        vid = "dQw4w9WgXcQ"
        for url in (f"https://www.youtube.com/watch?v={vid}", f"https://youtu.be/{vid}", f"https://m.youtube.com/watch?v={vid}&feature=share",
                    f"https://www.youtube.com/shorts/{vid}", f"https://music.youtube.com/watch?v={vid}", f"https://youtube.com/live/{vid}"):
            self.assertEqual(pr.youtube_target(url)[0], vid, url)

    def test_playlist_and_start_time(self):
        vid, playlist, start = pr.youtube_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghij12&t=1m30s")
        self.assertEqual((playlist, start), ("PLabcdefghij12", 90))
        self.assertEqual(pr.youtube_target("https://youtu.be/dQw4w9WgXcQ?t=75")[2], 75)

    def test_other_sites_and_lookalikes_are_not_youtube(self):
        for url in ("https://example.com/watch?v=dQw4w9WgXcQ", "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/", "https://www.youtube.com/watch?v=short", "ftp://youtu.be/dQw4w9WgXcQ", "not a link"):
            self.assertIsNone(pr.youtube_target(url), url)


class Clients(unittest.TestCase):
    def test_only_the_home_network_is_served(self):
        for ip in ("192.168.1.20", "10.0.0.5", "172.16.3.1", "127.0.0.1"):
            self.assertTrue(pr.private_client(ip), ip)
        for ip in ("8.8.8.8", "93.184.216.34", "garbage"):
            self.assertFalse(pr.private_client(ip), ip)


class Backend:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a):
            self.calls.append((name, *a))
            return {"state": {"tv": None, "volume": {}, "note": None}, "apps": [{"id": "12", "name": "Netflix"}],
                    "icon": b"\x89PNG", "cast": "ok"}.get(name)
        return call


class Server(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.srv = pr.PhoneRemote(self.backend, "secret-token", b"\x89PNGicon", port=0, bind="127.0.0.1")
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.port}"
        patcher = mock.patch("phone_remote.time.sleep")       # the wrong-token delay would only slow the tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.srv.stop)

    def call(self, path, body=None, token="secret-token"):
        headers = {"X-Token": token} if token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_page_only_gets_the_token_when_the_link_has_it(self):
        _, with_token = self.call("/?k=secret-token", token=None)
        _, without = self.call("/", token=None)
        _, wrong = self.call("/?k=nope", token=None)
        self.assertIn(b'TOKEN="secret-token"', with_token)
        self.assertNotIn(b"secret-token", without)
        self.assertNotIn(b"secret-token", wrong)

    def test_every_api_call_needs_the_token(self):
        for path, body in (("/api/state", None), ("/api/key", {"name": "Up"}), ("/api/cast", {"url": "https://x.com"}),
                           ("/api/apps", None)):
            self.assertEqual(self.call(path, body, token=None)[0], 401, path)
            self.assertEqual(self.call(path, body, token="wrong")[0], 401, path)
        self.assertEqual(self.backend.calls, [])

    def test_the_youtube_tab_asks_where_playlet_is(self):
        self.assertEqual(self.call("/api/playlet", token=None)[0], 401)
        self.srv.backend = type("B", (), {"playlet": lambda s: {"up": True, "url": "http://10.0.0.9:8888/", "id": "693751"}})()
        code, body = self.call("/api/playlet")
        self.assertEqual((code, json.loads(body)["url"]), (200, "http://10.0.0.9:8888/"))

    def test_the_page_has_a_youtube_tab_with_playlet_settings_behind_it(self):
        _, page = self.call("/?k=secret-token", token=None)
        for needle in (b'data-tab="yt"', b'id="plframe"', b"/api/playlet", b"/api/web/youtube"):
            self.assertIn(needle, page)

    def test_the_token_can_ride_in_the_url_for_images(self):
        code, body = self.call("/api/icon/12?k=secret-token", token=None)
        self.assertEqual((code, body), (200, b"\x89PNG"))
        self.assertEqual(self.backend.calls, [("icon", "12")])

    def test_keys_are_limited_to_the_list(self):
        self.assertEqual(self.call("/api/key", {"name": "Up"})[0], 200)
        for bad in ("PowerOff", "Power", "Lit_a", "", "up"):
            self.assertEqual(self.call("/api/key", {"name": bad})[0], 400, bad)
        self.assertEqual(self.backend.calls, [("key", "Up")])

    def test_inputs_are_checked(self):
        self.assertEqual(self.call("/api/seek", {"delta": 99999})[0], 400)
        self.assertEqual(self.call("/api/volume", {"step": 0})[0], 400)
        self.assertEqual(self.call("/api/volume", {"step": 500})[0], 400)
        self.assertEqual(self.call("/api/launch", {"id": "../../x"})[0], 400)
        self.assertEqual(self.call("/api/text", {"text": ""})[0], 400)
        self.assertEqual(self.call("/api/cast", {"url": ""})[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_good_calls_reach_the_backend(self):
        self.call("/api/seek", {"delta": -10})
        self.call("/api/volume", {"step": 2})
        self.call("/api/launch", {"id": "13535"})
        self.call("/api/text", {"text": "hello"})
        self.call("/api/stop", {})
        code, body = self.call("/api/cast", {"url": "https://example.com/video"})
        self.assertEqual((code, json.loads(body)), (200, {"message": "ok"}))
        self.assertEqual(self.backend.calls, [("seek", -10.0), ("volume", 2), ("launch", "13535"), ("text", "hello"),
                                              ("stop_cast",), ("cast", "https://example.com/video")])

    def test_a_backend_failure_comes_back_as_a_message(self):
        class Broken(Backend):
            def key(self, name):
                raise RuntimeError("Couldn't reach the Roku")
        self.srv.backend = Broken()
        code, body = self.call("/api/key", {"name": "Up"})
        self.assertEqual((code, json.loads(body)["error"]), (502, "Couldn't reach the Roku"))

    def test_junk_bodies_are_refused(self):
        req = urllib.request.Request(self.base + "/api/key", data=b"[1,2]", headers={"X-Token": "secret-token"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_a_stranger_off_the_home_network_gets_nothing(self):
        with mock.patch("phone_remote.private_client", return_value=False):
            self.assertEqual(self.call("/api/state")[0], 403)
            self.assertEqual(self.call("/?k=secret-token", token=None)[0], 403)

    def test_manifest_and_icon(self):
        self.assertEqual(json.loads(self.call("/manifest.webmanifest?k=secret-token", token=None)[1])["start_url"], "/?k=secret-token")
        self.assertEqual(self.call("/manifest.webmanifest", token=None)[0], 401)
        self.assertEqual(self.call("/icon.png", token=None), (200, b"\x89PNGicon"))

    def test_a_new_token_cuts_the_old_link_off(self):
        self.srv.token = "fresh"
        self.assertEqual(self.call("/api/state")[0], 401)
        self.assertEqual(self.call("/api/state", token="fresh")[0], 200)

    def test_it_can_be_switched_off_and_on_again(self):
        self.srv.stop()
        import time
        time.sleep(0.3)
        self.assertFalse(self.srv.running)
        self.srv.start()
        self.assertTrue(self.srv.running)



# ---- the pieces that live inside the app ------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from test_web_tab_cast import Base  # noqa: E402


class LinkCast(Base):
    """A link sent from the phone: open it in the Web tab's browser, wait for a video, then cast like the button."""

    def setUp(self):
        super().setUp()
        t = self.tab
        self.navigated, self.cast_calls, self.launched = [], [], []
        self.fresh = False
        t.home, t.link = "https://duckduckgo.com", None
        t.ui.GREEN = "g"
        t._post = lambda fn: fn()
        t.launch = lambda: self.launched.append(1)
        t.cast = lambda: self.cast_calls.append(1)
        t.guard.navigate = self.navigated.append
        t.guard.has_fresh_stream = lambda: self.fresh
        t.app.after = lambda ms, fn: None            # the test drives the steps by hand

    def test_a_link_is_opened_then_cast_once_the_video_has_started(self):
        t = self.tab
        self.assertIn("TV", t.cast_link("https://site.example/ep1"))
        self.assertEqual(self.navigated, [])          # navigation happens on the next step, once the browser is ready
        t._link_step()
        self.assertEqual(self.navigated, ["https://site.example/ep1"])
        t._link_step()
        self.assertEqual(self.cast_calls, [])         # no stream yet
        self.fresh = True
        t._link_step()
        self.assertEqual(self.cast_calls, [])         # seen, but let the player settle
        url, t0, nav, seen = t.link
        t.link = (url, t0, nav, seen - 4)
        t._link_step()
        self.assertEqual(self.cast_calls, [1])
        self.assertIsNone(t.link)

    def test_with_no_video_it_still_tries_after_the_wait(self):
        t = self.tab
        t.cast_link("https://site.example/ep1")
        t._link_step()
        url, t0, nav, seen = t.link
        t.link = (url, t0 - 60, nav, seen)
        t._link_step()
        self.assertEqual(self.cast_calls, [1])        # cast() itself explains "press play" or falls back to yt-dlp

    def test_a_browser_that_never_starts_gives_up_with_a_message(self):
        t = self.tab
        t.guard.snapshot = lambda: {"unsupported": "", "url": "", "title": "", "running": False}
        t.cast_link("https://site.example/ep1")
        t.link = (t.link[0], t.link[1] - 60, False, 0.0)
        t._link_step()
        self.assertIsNone(t.link)
        self.assertEqual(self.cast_calls, [])
        self.assertIn("didn't start", t.note[0])

    def test_refusals_come_back_to_the_phone(self):
        t = self.tab
        with self.assertRaises(ValueError):
            t.cast_link("https://www.netflix.com/watch/123")
        t.cast_link("javascript:alert(1)")                # not a link: it becomes a search, never a script URL
        t._link_step()
        self.assertTrue(self.navigated[0].startswith("https://duckduckgo.com/?q="))
        t.link = None
        t.busy = True
        with self.assertRaises(ValueError):
            t.cast_link("https://site.example/x")
        self.assertIsNone(t.link)

    def test_a_press_of_cast_on_the_pc_takes_over(self):
        t = self.tab
        t.cast_link("https://site.example/ep1")
        t.busy = True
        t._link_step()
        self.assertIsNone(t.link)
        self.assertEqual(self.navigated, [])

    def test_phone_note_only_while_the_note_is_live(self):
        import time
        t = self.tab
        t.note, t.note_until = ("Found it", "g"), time.time() + 5
        self.assertEqual(t.phone_note(), ("Found it", True))
        t.note_until = 0
        self.assertIsNone(t.phone_note())


class AppBackend(unittest.TestCase):
    def setUp(self):
        import roku_listen_gui as g
        self.g = g
        self.keys, self.after = [], []
        self.watching = None
        web = SimpleNamespace(phone_note=lambda: None, busy=False, sync=None, cast_link=lambda u: "queued " + u,
                              phone_watching=lambda: self.watching,
                              phone_transport=lambda action, value=0: self.keys.append(("page", action, value)),
                              cancel_cast=lambda: self.after.append("cancel"), stop_casting=lambda: self.after.append("stop"))
        self.app = SimpleNamespace(
            sync=SimpleNamespace(note="", note_until=0.0, note_ok=True),
            web=web, keys=SimpleNamespace(key=self.keys.append, text=lambda s: self.keys.append(("text", s))),
            tvvol=SimpleNamespace(level=30, muted=False), listener=None, pc_volume=100, last_tv={"state": "play", "app": "Netflix", "pos": 30.0, "dur": 600.0},
            _host="10.0.0.5", after=lambda ms, fn: fn(), _phone_pc_volume=lambda d: self.keys.append(("pc", d)))
        self.b = g.PhoneBackend(self.app)

    def test_a_youtube_link_goes_to_playlet_and_anything_else_to_the_web_tab(self):
        with mock.patch.object(self.g.Remote, "cast") as cast:
            self.assertEqual(self.b.cast("https://youtu.be/dQw4w9WgXcQ?t=30"), "Playing on the TV")
            cast.assert_called_once_with("10.0.0.5", "dQw4w9WgXcQ", 30, fresh=True)
        self.assertEqual(self.b.cast("https://site.example/ep"), "queued https://site.example/ep")

    def test_volume_follows_where_the_sound_is(self):
        self.b.volume(2)
        self.b.volume(-1)
        self.assertEqual(self.keys, ["VolumeUp", "VolumeUp", "VolumeDown"])
        self.app.listener = object()
        self.keys.clear()
        self.b.volume(2)
        self.assertEqual(self.keys, [("pc", 10)])
        self.assertEqual(self.b.state()["volume"], {"where": "pc", "level": 100, "muted": False})

    def test_while_casting_play_and_skip_go_through_the_page_not_the_tv(self):
        with mock.patch.object(self.g.Remote, "seek") as seek:
            self.b.key("Play")
            self.b.seek(-10)
            self.assertEqual(self.keys, ["Play"])               # not casting: the Roku's own keys
            seek.assert_called_once_with("10.0.0.5", -10)
            self.keys.clear()
            self.watching = {"id": 1.0, "title": "Ghost in the Shell", "url": "https://x/y"}
            self.b.key("Play")
            self.b.seek(10)
            self.b.key("Back")                                  # everything else still goes to the Roku
            self.assertEqual(self.keys, [("page", "toggle", 0), ("page", "skip", 10), "Back"])
            seek.assert_called_once()

    def test_state_reports_the_tv_and_the_web_note(self):
        st = self.b.state()
        self.assertEqual((st["tv"]["app"], st["volume"], st["watching"], st["note"]),
                         ("Netflix", {"where": "tv", "level": 30, "muted": False}, None, None))
        self.watching = {"id": 2.0, "title": "T", "url": "u"}
        self.assertEqual(self.b.state()["watching"]["title"], "T")
        self.watching = None
        self.app.web.phone_note = lambda: ("Found it", True)
        self.assertEqual(self.b.state()["note"], {"text": "Found it", "ok": True})

    def test_stop_cancels_a_cast_in_progress_or_stops_the_running_one(self):
        self.b.stop_cast()
        self.app.web.busy = True
        self.b.stop_cast()
        self.assertEqual(self.after, ["stop", "cancel"])

if __name__ == "__main__":
    unittest.main()
