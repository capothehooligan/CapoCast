import functools
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subtitles as sub  # noqa: E402
import upscaler  # noqa: E402
import web_tab  # noqa: E402
from stream_finder import Candidate  # noqa: E402

VTT = "WEBVTT\n\n00:00:02.000 --> 00:00:05.000\nHello there\n\n00:00:12.000 --> 00:00:16.000\n<i>Second line</i>\n"


class Finding(unittest.TestCase):
    def test_subtitle_files_are_told_by_name_or_type(self):
        for url, mime in (("https://c/en.vtt?x=1", ""), ("https://c/a/b.srt", ""), ("https://c/ep1.ass", ""),
                          ("https://c/sub", "text/vtt"), ("https://c/x", "application/x-subrip; charset=utf-8")):
            self.assertTrue(sub.is_subtitle_url(url, mime), url)

    def test_thumbnail_index_files_are_not_subtitles(self):
        for url in ("https://c/thumbnails.vtt", "https://c/storyboard/1.vtt", "https://c/preview-sprite.vtt"):
            self.assertFalse(sub.is_subtitle_url(url), url)
        self.assertFalse(sub.is_subtitle_url("https://c/video.m3u8", "application/vnd.apple.mpegurl"))

    def test_languages_from_labels_and_from_addresses(self):
        self.assertEqual(sub.language_of("en"), "English")
        self.assertEqual(sub.language_of("Português"), "")                       # not in the table: no guess
        self.assertEqual(sub.language_of("https://c/subs/eng-3.vtt", strict=True), "English")
        self.assertEqual(sub.language_of("https://c/subs/en.vtt", strict=True), "English")
        self.assertEqual(sub.language_of("https://c/api?id=5/it/x.vtt", strict=True), "")     # 'id' and 'it' aren't languages here

    def test_a_master_playlist_lists_its_subtitle_tracks(self):
        text = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="English",LANGUAGE="en",URI="subs/en.m3u8"\n'
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="Audio",URI="a.m3u8"\n'
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="Español",LANGUAGE="es",URI="https://o/es.m3u8"\n')
        got = sub.hls_renditions(text, "https://cdn/v/master.m3u8")
        self.assertEqual([(r["label"], r["lang"], r["url"]) for r in got],
                         [("English", "English", "https://cdn/v/subs/en.m3u8"), ("Español", "Spanish", "https://o/es.m3u8")])

    def test_a_stream_that_is_not_a_master_offers_nothing(self):
        self.assertEqual(sub.fetch_renditions("https://c/v.mp4", {}), [])

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self, n):
                return b"#EXTM3U\n#EXTINF:4,\nseg.ts\n"
        self.assertEqual(sub.fetch_renditions("https://c/v.m3u8", {}, opener=lambda req, timeout: Reply()), [])


class Choosing(unittest.TestCase):
    def tracks(self):
        files = [{"url": "https://c/eng-1.vtt", "headers": {"Referer": "https://site/"}},
                 {"url": "https://c/2.vtt", "headers": {"Referer": "https://site/"}},
                 {"url": "https://c/eng-2.vtt", "headers": {}}]
        dom = [{"src": "https://c/2.vtt", "label": "Français", "lang": "fr", "kind": "subtitles", "def": False},
               {"src": "https://c/ch.vtt", "label": "Chapters", "lang": "en", "kind": "chapters"},
               {"src": "https://c/3.vtt", "label": "Deutsch", "lang": "de", "kind": "captions", "def": True}]
        return sub.build_tracks(files, dom, [{"url": "https://c/r.m3u8", "label": "Español", "lang": "Spanish"}], {"X": "1"})

    def test_everything_offered_is_listed_once_with_names(self):
        labels = [(t["label"], t["lang"]) for t in self.tracks()]
        self.assertEqual(labels[0], ("Deutsch", "German"))                       # the page's own default comes first
        self.assertIn(("Français", "French"), labels)
        self.assertIn(("English", "English"), labels)
        self.assertIn(("English 2", "English"), labels)                          # two files, one name: told apart
        self.assertIn(("Español", "Spanish"), labels)
        self.assertNotIn("Chapters", [t[0] for t in labels])                     # chapter tracks are not subtitles

    def test_a_file_the_browser_fetched_keeps_the_headers_it_used(self):
        fr = [t for t in self.tracks() if t["label"] == "Français"][0]
        self.assertEqual(fr["headers"], {"Referer": "https://site/"})

    def test_tracks_with_no_headers_borrow_the_streams(self):
        de = [t for t in self.tracks() if t["label"] == "Deutsch"][0]
        self.assertEqual(de["headers"], {"X": "1"})

    def test_the_remembered_choice_matches_a_label_or_a_language(self):
        tracks = self.tracks()
        self.assertEqual(sub.pick_track(tracks, "english")["url"], "https://c/eng-1.vtt")
        self.assertEqual(sub.pick_track(tracks, "english 2")["url"], "https://c/eng-2.vtt")
        self.assertEqual(sub.pick_track(tracks, "spanish")["url"], "https://c/r.m3u8")
        self.assertIsNone(sub.pick_track(tracks, "klingon"))
        self.assertIsNone(sub.pick_track(tracks, ""))


class Converting(unittest.TestCase):
    def test_the_default_style_becomes_readable_but_a_fansubs_own_style_is_kept(self):
        ass = ("[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial,16,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,"
               "0,0,1,1,0,2,10,10,10,1\n\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\n")
        out = sub.restyle(ass)
        self.assertIn("Style: Default,Roboto,19,", out)
        self.assertIn("Dialogue: 0,0:00:01.00", out)

    def test_ffmpeg_failures_come_back_as_false_not_exceptions(self):
        out = os.path.join(tempfile.mkdtemp(), "o.ass")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        bad = SimpleNamespace(returncode=1, stderr=b"HTTP error 403")
        self.assertFalse(sub.to_ass({"url": "https://c/a.vtt", "headers": {}}, out, run=lambda *a, **k: bad, exe="ffmpeg"))
        self.assertFalse(sub.to_ass({"url": "https://c/a.vtt", "headers": {}}, out, run=lambda *a, **k: 1 / 0, exe="ffmpeg"))
        self.assertFalse(sub.to_ass({"url": "https://c/a.vtt", "headers": {}}, out, exe=None) if not shutil.which("ffmpeg") else False)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_real_vtt_over_http_becomes_a_styled_ass(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(folder, "en.vtt"), "w", encoding="utf-8") as f:
            f.write(VTT)
        handler = functools.partial(SimpleHTTPRequestHandler, directory=folder)
        handler.log_message = lambda *a: None
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        out = os.path.join(folder, "out.ass")
        self.assertTrue(sub.to_ass({"url": f"http://127.0.0.1:{srv.server_port}/en.vtt", "headers": {}}, out))
        text = open(out, encoding="utf-8").read()
        self.assertIn("Style: Default,Roboto,19,", text)
        self.assertEqual(text.count("Dialogue:"), 2)
        self.assertIn("{\\i1}Second line", text)


class Burning(unittest.TestCase):
    def test_subtitles_only_makes_no_use_of_the_shader_or_the_gpu_filter_graph(self):
        cmd = upscaler.command("ffmpeg", "https://c/v.mp4", {}, "mp4", 5, True, "burn", True)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertNotIn("libplacebo", vf)
        self.assertNotIn("-init_hw_device", cmd)
        self.assertEqual(vf, "setpts=PTS+20/TB,subtitles=../subs.ass:fontsdir=../fonts,setpts=PTS-20/TB,format=yuv420p")

    def test_upscaling_draws_the_words_after_the_upscale_so_they_stay_sharp(self):
        cmd = upscaler.command("ffmpeg", "https://c/v.mp4", {}, "mp4", 0, True, "hq", True)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertLess(vf.index("libplacebo"), vf.index("subtitles="))
        self.assertIn("setpts=PTS+0/TB", vf)
        self.assertIn("-init_hw_device", cmd)

    def test_without_subtitles_the_command_is_what_it_always_was(self):
        cmd = upscaler.command("ffmpeg", "https://c/v.mp4", {}, "mp4", 0, True)
        self.assertNotIn("subtitles", " ".join(cmd))

    def test_a_subtitles_only_cast_does_not_claim_a_resolution(self):
        self.assertNotIn("RESOLUTION", upscaler.master_playlist(True, "burn"))
        self.assertIn("RESOLUTION=3840x2160", upscaler.master_playlist(True, "hq"))

    def test_what_a_pc_needs_is_said_in_words(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIn("Subtitles need FFmpeg", upscaler.missing("burn", True))
            self.assertIn("Upscaling needs FFmpeg", upscaler.missing())


class Tab(unittest.TestCase):
    def setUp(self):
        self.notes = []
        tab = self.tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.app = SimpleNamespace(after=lambda ms, fn: fn(), _save=lambda: None)
        tab.ui = SimpleNamespace(log=lambda m: None, AMBER="a", MUTED="m", GREEN="g", ACCENT="acc")
        tab._note = lambda text, *a: self.notes.append(text)
        tab.upscale, tab.upscaled, tab.attempt, tab.subs = "off", None, 1, ""
        patch = mock.patch.object(upscaler, "missing", lambda *a: "")
        patch.start()
        self.addCleanup(patch.stop)

    def cand(self, **kw):
        base = dict(url="https://c/x.m3u8", kind="hls", duration=1400.0, height=1080, width=1920, has_audio=True, has_video=True)
        return Candidate(**{**base, **kw})

    def test_subtitles_without_upscaling_are_a_burn_only_cast(self):
        self.assertEqual(self.tab._upscale_plan(self.cand(), "/tmp/s.ass"),
                         {"mode": "burn", "duration": 1400.0, "audio": True, "subs": "/tmp/s.ass", "hdr": False})

    def test_subtitles_with_upscaling_do_both(self):
        self.tab.upscale = "hq"
        plan = self.tab._upscale_plan(self.cand(), "/tmp/s.ass")
        self.assertEqual((plan["mode"], plan["subs"]), ("hq", "/tmp/s.ass"))

    def test_a_video_already_in_4k_still_gets_its_subtitles(self):
        self.tab.upscale = "hq"
        plan = self.tab._upscale_plan(self.cand(height=2160), "/tmp/s.ass")
        self.assertEqual(plan["mode"], "burn")

    def test_neither_means_a_plain_cast(self):
        self.assertIsNone(self.tab._upscale_plan(self.cand(), ""))

    def test_a_pc_that_cannot_upscale_still_adds_subtitles(self):
        self.tab.upscale = "hq"
        with mock.patch.object(upscaler, "missing", lambda mode="fast", subs=False, hdr=False: "no shaders" if mode != "burn" else ""):
            plan = self.tab._upscale_plan(self.cand(), "/tmp/s.ass")
        self.assertEqual(plan["mode"], "burn")

    def test_skipping_says_why_in_terms_of_subtitles(self):
        for cand, words in ((self.cand(live=True), "can't have subtitles"), (self.cand(has_video=False), "add subtitles to")):
            self.notes.clear()
            self.assertIsNone(self.tab._upscale_plan(cand, "/tmp/s.ass"))
            self.assertIn(words, self.notes[0])

    def test_the_status_line_says_subtitles(self):
        calls = []
        self.tab.discord = SimpleNamespace(show=lambda *a, **k: calls.append(a))
        self.tab.cast_since, self.tab.cast_end = 0.0, None
        self.tab.upscaled = {"mode": "burn", "subs": "/tmp/s.ass"}
        self.tab._set_presence("Heat", None, "FMovies")
        self.assertEqual(calls[0][2], "on FMovies · subtitles")
        self.tab.upscaled = {"mode": "hq", "subs": "/tmp/s.ass"}
        self.tab._set_presence("Heat", None, "FMovies")
        self.assertEqual(calls[1][2], "on FMovies · Anime4K HQ upscale · subtitles")


class Control(unittest.TestCase):
    """Clicking the Subtitles label."""

    def setUp(self):
        self.notes, self.saved = [], []
        tab = self.tab = web_tab.WebTab.__new__(web_tab.WebTab)
        tab.app = SimpleNamespace(_save=lambda: self.saved.append(1))
        tab.ui = SimpleNamespace(log=lambda m: None, AMBER="a", MUTED="m", ACCENT="acc")
        tab._note = lambda text, *a: self.notes.append(text)
        tab.subs_lbl = SimpleNamespace(config=lambda **k: setattr(self, "label", k))
        self.files = [{"url": "https://c/eng.vtt", "headers": {}}, {"url": "https://c/spa.vtt", "headers": {}}]
        tab.guard = SimpleNamespace(subtitle_sources=lambda dur=0: (self.files, []))
        patch = mock.patch.object(upscaler, "missing", lambda *a: "")
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_steps_through_off_and_each_language_and_remembers_the_choice(self):
        seen = []
        for _ in range(4):
            self.tab.next_subs()
            seen.append(self.tab.subs)
        self.assertEqual(seen, ["english", "spanish", "", "english"])
        self.assertEqual(self.label["text"], "Subtitles: English")
        self.assertTrue(self.saved)

    def test_a_page_with_no_subtitle_files_says_how_to_get_some(self):
        self.files.clear()
        self.tab.next_subs()
        self.assertEqual(self.tab.subs, "")
        self.assertIn("captions on", self.notes[0])

    def test_a_language_the_page_does_not_have_can_be_switched_off(self):
        self.files.clear()
        self.tab.subs = "english"
        self.tab.next_subs()
        self.assertEqual(self.tab.subs, "")

    def test_a_pc_without_what_it_needs_cannot_turn_it_on(self):
        with mock.patch.object(upscaler, "missing", lambda *a: "needs FFmpeg"):
            self.tab.next_subs()
        self.assertEqual(self.tab.subs, "")
        self.assertIn("needs FFmpeg", self.notes[0])

    def test_discord_follows_the_tv_with_the_streams_offset_added(self):
        calls = []
        tab = self.tab
        tab.discord = SimpleNamespace(showing="web", progress=lambda *a: calls.append(a))
        tab.SOURCE = "web"
        tab.sync = SimpleNamespace(active=True, live=False, settle_until=0, offset=100.0, dur=1400.0)
        tab._feed_clock({"state": "play", "app": "RokuCast", "pos": 20.0, "dur": 1300.0})
        tab._feed_clock({"state": "pause", "app": "RokuCast", "pos": 25.0, "dur": 1300.0})
        tab._feed_clock({"state": "buffer", "app": "RokuCast", "pos": 25.0, "dur": 1300.0})   # in between: left alone
        tab._feed_clock({"state": "play", "app": "Netflix", "pos": 25.0, "dur": 1300.0})      # elsewhere: left alone
        self.assertEqual(calls, [("web", 120.0, 1400.0, True), ("web", 125.0, 1400.0, False)])

    def test_a_restart_in_progress_does_not_move_the_clock(self):
        calls = []
        tab = self.tab
        tab.discord = SimpleNamespace(showing="web", progress=lambda *a: calls.append(a))
        tab.SOURCE = "web"
        tab.sync = SimpleNamespace(active=True, live=False, settle_until=10 ** 12, offset=0, dur=100)
        tab._feed_clock({"state": "play", "app": "RokuCast", "pos": 20.0, "dur": 100})
        tab.sync.settle_until = 0
        tab.sync.live = True
        tab._feed_clock({"state": "play", "app": "RokuCast", "pos": 20.0, "dur": 100})        # a live stream has no position
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
