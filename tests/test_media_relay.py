import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_relay  # noqa: E402
from drm import DrmError, refusal_for  # noqa: E402
from media_relay import MediaRelay, probe_all, probe_dash, probe_hls, replay_headers, rewrite_hls, trim_hls  # noqa: E402
from stream_finder import Candidate  # noqa: E402

REFERER = "https://anime.example/"
MOVIE = bytes(range(256)) * 400                      # 102,400 bytes
SEG = b"\x47" + b"seg" * 200

MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nv720/index.m3u8\n"
MEDIA = ('#EXTM3U\n#EXT-X-TARGETDURATION:100\n#EXT-X-KEY:METHOD=AES-128,URI="../key.bin"\n'
         + "".join(f"#EXTINF:100.0,\nseg{i}.jpg\n" for i in range(3)) + "#EXT-X-ENDLIST\n")


class Site(BaseHTTPRequestHandler):
    """A stand-in video site that refuses requests without the right Referer, like many real ones."""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Site.seen.append(dict(self.headers))
        if self.headers.get("Referer") != REFERER:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        path = self.path.split("?")[0]
        body, ctype, status, extra = b"", "application/octet-stream", 200, {}
        if path == "/master.m3u8":
            body, ctype = MASTER.encode(), "application/vnd.apple.mpegurl"
        elif path == "/v720/index.m3u8":
            body, ctype = MEDIA.encode(), "application/vnd.apple.mpegurl"
        elif path.startswith("/v720/seg"):
            body, ctype = SEG, "image/jpeg"
        elif path == "/key.bin":
            body = b"k" * 16
        elif path == "/movie.mp4":
            ctype, body = "video/mp4", MOVIE
            rng = self.headers.get("Range")
            if rng:
                a, _, b = rng.replace("bytes=", "").partition("-")
                a, b = int(a), int(b) if b else len(MOVIE) - 1
                body, status = MOVIE[a:b + 1], 206
                extra["Content-Range"] = f"bytes {a}-{b}/{len(MOVIE)}"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        rng = self.headers.get("Range")
        if rng and path.endswith(".m3u8"):           # like the real CDN behind strm.cx: it honours ranges on playlists
            a, _, b = rng.replace("bytes=", "").partition("-")
            a, b = int(a), min(int(b) if b else len(body) - 1, len(body) - 1)
            extra["Content-Range"] = f"bytes {a}-{b}/{len(body)}"
            body, status = body[a:b + 1], 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.site.server_address[1]}"
        cls.relay = MediaRelay("127.0.0.1", allow=lambda: "192.168.1.50", allow_loopback=True)
        cls.relay.start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.site.shutdown()

    def get(self, url, headers=None):
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10)

    def test_hls_is_rewritten_and_every_piece_replays_the_referer(self):
        url = self.relay.add(self.origin + "/master.m3u8", {"Referer": REFERER}, "hls")
        self.assertTrue(url.endswith("/index.m3u8"))
        master = self.get(url).read().decode()
        variant = [l for l in master.splitlines() if l and not l.startswith("#")][0]
        self.assertTrue(variant.startswith("/s/") and variant.endswith(".m3u8"))
        self.assertNotIn(self.origin, master)
        media = self.get(self.relay.base + variant).read().decode()
        lines = media.splitlines()
        key = [l for l in lines if l.startswith("#EXT-X-KEY")][0]
        self.assertIn('URI="/s/', key)
        seg = [l for l in lines if l and not l.startswith("#")][0]
        self.assertTrue(seg.endswith(".ts"), seg)          # the site called it .jpg; Roku sees .ts
        resp = self.get(self.relay.base + seg)
        self.assertEqual(resp.read(), SEG)
        self.assertEqual(resp.headers["Content-Type"], "video/mp2t")
        self.assertEqual(self.get(self.relay.base + key.split('URI="')[1].rstrip('"')).read(), b"k" * 16)

    def test_a_range_request_for_a_playlist_gets_the_whole_playlist(self):
        # The Roku's reader can ask for part of a playlist. Passing that on to a site that honours it gave the TV a
        # fragment of the master, which it refused: "reader pick stream error:bad:invalid or corrupt playlist".
        url = self.relay.add(self.origin + "/master.m3u8", {"Referer": REFERER}, "hls")
        resp = self.get(url, {"Range": "bytes=0-20"})
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.headers.get("Content-Range"))
        master = resp.read().decode()
        variant = [l for l in master.splitlines() if l and not l.startswith("#")][0]
        self.assertIn("RESOLUTION=1280x720", master)
        media = self.get(self.relay.base + variant, {"Range": "bytes=0-30"}).read().decode()
        self.assertIn("#EXT-X-ENDLIST", media)
        self.assertEqual(sum(1 for l in media.splitlines() if l.startswith("/s/") and l.endswith(".ts")), 3)
        self.assertNotIn("Range", Site.seen[-1])               # the site was asked for all of it
        seg = [l for l in media.splitlines() if l.startswith("/s/") and l.endswith(".ts")][0]
        self.assertEqual(self.get(self.relay.base + seg, {"Range": "bytes=0-"}).read(), SEG)

    def test_mp4_supports_range(self):
        url = self.relay.add(self.origin + "/movie.mp4", {"Referer": REFERER}, "mp4")
        resp = self.get(url, {"Range": "bytes=1000-1999"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.read(), MOVIE[1000:2000])
        self.assertEqual(resp.headers["Content-Range"], f"bytes 1000-1999/{len(MOVIE)}")

    def test_head(self):
        url = self.relay.add(self.origin + "/movie.mp4", {"Referer": REFERER}, "mp4")
        resp = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10)
        self.assertEqual(resp.headers["Content-Length"], str(len(MOVIE)))

    def test_without_the_headers_the_site_refuses(self):
        url = self.relay.add(self.origin + "/movie.mp4", {}, "mp4")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url)
        self.assertEqual(cm.exception.code, 403)

    def test_unknown_token_and_path(self):
        for path in ("/s/deadbeefdeadbeef/index.m3u8", "/", "/s/x", "/other"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(self.relay.base + path)
            self.assertEqual(cm.exception.code, 404, path)

    def test_other_devices_are_refused(self):
        strict = MediaRelay("127.0.0.1", allow=lambda: "192.168.1.50", allow_loopback=False)
        strict.start()
        try:
            url = strict.add(self.origin + "/movie.mp4", {"Referer": REFERER}, "mp4")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(url)
            self.assertEqual(cm.exception.code, 403)
        finally:
            strict.stop()

    def test_probe_reads_length_and_height(self):
        c = Candidate(url=self.origin + "/master.m3u8", kind="hls", headers={"Referer": REFERER})
        covered = probe_hls(c)
        self.assertTrue(c.master)
        self.assertEqual(c.height, 720)
        self.assertEqual(c.duration, 300)
        self.assertEqual(covered, [self.origin + "/v720/index.m3u8"])

    def test_session_hits_are_counted(self):
        url = self.relay.add(self.origin + "/movie.mp4", {"Referer": REFERER}, "mp4")
        self.get(url).read()
        self.assertEqual(self.relay.session_for(url).hits, 1)


MPD_REL = ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
           'mediaPresentationDuration="PT10M"><Period><AdaptationSet mimeType="video/mp4"><SegmentTemplate '
           'media="video/seg_$Number$.m4s" initialization="video/init.mp4" startNumber="1"/><Representation id="1" '
           'height="720" bandwidth="1"/></AdaptationSet></Period></MPD>')
MPD_ABS = MPD_REL.replace('media="video/seg_$Number$.m4s" initialization="video/init.mp4"',
                          'media="http://127.0.0.1:{port}/video/seg_$Number$.m4s" '
                          'initialization="http://127.0.0.1:{port}/video/init.mp4"')


class DashSite(BaseHTTPRequestHandler):
    """A DASH site: a manifest at /dash/manifest.mpd and segments that insist on the Referer."""
    manifest = MPD_REL

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if self.headers.get("Referer") != REFERER:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if path == "/dash/manifest.mpd":
            body, ctype = DashSite.manifest.replace("{port}", str(self.server.server_address[1])).encode(), "application/dash+xml"
        elif path in ("/dash/video/init.mp4", "/dash/video/seg_1.m4s", "/video/init.mp4", "/video/seg_1.m4s"):
            body, ctype = b"dash-bytes-" + path.encode(), "video/mp4"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), DashSite)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.site.server_address[1]}"
        cls.relay = MediaRelay("127.0.0.1", allow=lambda: "192.168.1.50", allow_loopback=True)
        cls.relay.start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.site.shutdown()

    def get(self, url):
        return urllib.request.urlopen(urllib.request.Request(url), timeout=10)

    def test_relative_segments_resolve_against_the_manifest_with_the_referer(self):
        DashSite.manifest = MPD_REL
        url = self.relay.add(self.origin + "/dash/manifest.mpd", {"Referer": REFERER}, "dash")
        self.assertTrue(url.endswith("/manifest.mpd"))
        mpd = self.get(url).read().decode()
        self.assertIn('media="video/seg_$Number$.m4s"', mpd)               # relative stays relative
        base = url.rsplit("/", 1)[0]
        self.assertEqual(self.get(base + "/video/init.mp4").read(), b"dash-bytes-/dash/video/init.mp4")
        self.assertEqual(self.get(base + "/video/seg_1.m4s?x=1").read(), b"dash-bytes-/dash/video/seg_1.m4s")

    def test_absolute_urls_in_the_manifest_are_pointed_at_the_relay(self):
        DashSite.manifest = MPD_ABS
        url = self.relay.add(self.origin + "/dash/manifest.mpd", {"Referer": REFERER}, "dash")
        mpd = self.get(url).read().decode()
        self.assertNotIn("127.0.0.1:" + self.origin.rsplit(":", 1)[1], mpd.replace(self.relay.base, ""))
        self.assertIn('media="/s/', mpd)
        seg = [p for p in mpd.split('"') if p.startswith("/s/") and "seg_" in p][0].replace("$Number$", "1")
        self.assertEqual(self.get(self.relay.base + seg).read(), b"dash-bytes-/video/seg_1.m4s")

    def test_it_never_becomes_an_open_proxy(self):
        DashSite.manifest = MPD_REL
        url = self.relay.add(self.origin + "/dash/manifest.mpd", {"Referer": REFERER}, "dash")
        self.get(url).read()
        base = url.rsplit("/", 1)[0]
        for evil in ("/http://example.com/x", "/..%2f..%2f", "//example.com/x", "/b9/x"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(base + evil)
            self.assertIn(cm.exception.code, (403, 404), evil)

    def test_drm_manifest_is_refused(self):
        DashSite.manifest = MPD_REL.replace('<SegmentTemplate', '<ContentProtection schemeIdUri="urn:uuid:EDEF8BA9-79D6-'
                                            '4ACE-A3C8-27DCD51D21ED"/><SegmentTemplate')
        url = self.relay.add(self.origin + "/dash/manifest.mpd", {"Referer": REFERER}, "dash")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url)
        self.assertEqual(cm.exception.code, 451)
        self.assertIn("DRM", self.relay.session_for(url).error)

    def test_probe_dash(self):
        DashSite.manifest = MPD_REL
        c = Candidate(url=self.origin + "/dash/manifest.mpd", kind="dash", headers={"Referer": REFERER})
        probe_dash(c)
        self.assertEqual((c.duration, c.height), (600, 720))


KEYED = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-PLAYLIST-TYPE:VOD\n"
         '#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXT-X-MAP:URI="init.mp4"\n'
         + "".join(f"#EXTINF:10.0,\nseg{i}.ts\n" for i in range(6)) + "#EXT-X-ENDLIST\n")


class TrimTests(unittest.TestCase):
    def segs(self, text):
        return [l for l in text.splitlines() if l.startswith("seg")]

    def test_begins_at_the_segment_containing_the_target(self):
        out, skipped = trim_hls(KEYED, 25)
        self.assertEqual(skipped, 20.0)                             # segments 0 and 1 (0-20 s) are gone, 2 (20-30) stays
        self.assertEqual(self.segs(out), ["seg2.ts", "seg3.ts", "seg4.ts", "seg5.ts"])

    def test_the_key_and_init_segment_still_apply_after_the_cut(self):
        out, _ = trim_hls(KEYED, 35)
        self.assertIn('#EXT-X-KEY:METHOD=AES-128,URI="k.key"', out)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', out)
        self.assertLess(out.index("#EXT-X-KEY"), out.index("seg3.ts"))

    def test_the_media_sequence_moves_with_the_cut(self):
        out, _ = trim_hls(KEYED, 35)                                # 3 segments skipped: sequence 5 -> 8
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:8", out)
        self.assertNotIn("#EXT-X-MEDIA-SEQUENCE:5", out)

    def test_a_playlist_with_no_sequence_gets_one(self):
        plain = KEYED.replace("#EXT-X-MEDIA-SEQUENCE:5\n", "")
        out, _ = trim_hls(plain, 15)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", out)

    def test_the_end_marker_and_header_survive(self):
        out, _ = trim_hls(KEYED, 25)
        self.assertTrue(out.startswith("#EXTM3U"))
        self.assertIn("#EXT-X-ENDLIST", out)
        self.assertIn("#EXT-X-TARGETDURATION:10", out)

    def test_seeking_past_the_end_keeps_the_last_segment(self):
        out, skipped = trim_hls(KEYED, 9999)
        self.assertEqual(self.segs(out), ["seg5.ts"])
        self.assertEqual(skipped, 50.0)

    def test_live_and_zero_start_are_left_alone(self):
        live = KEYED.replace("#EXT-X-ENDLIST\n", "")
        self.assertEqual(trim_hls(live, 25), (live, 0.0))
        self.assertEqual(trim_hls(KEYED, 0), (KEYED, 0.0))

    def test_a_master_playlist_is_left_alone(self):
        self.assertEqual(trim_hls(MASTER, 30), (MASTER, 0.0))


class RelayStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.site.server_address[1]}"
        cls.relay = MediaRelay("127.0.0.1", allow=lambda: "192.168.1.50", allow_loopback=True)
        cls.relay.start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()
        cls.site.shutdown()

    def test_the_relay_serves_a_playlist_that_begins_at_the_start_time(self):
        url = self.relay.add(self.origin + "/master.m3u8", {"Referer": REFERER}, "hls", start=110)
        sess = self.relay.session_for(url)
        self.assertIsNone(sess.actual_start)                        # not known until the playlist has been served
        master = urllib.request.urlopen(url, timeout=10).read().decode()
        variant = [l for l in master.splitlines() if l and not l.startswith("#")][0]
        media = urllib.request.urlopen(self.relay.base + variant, timeout=10).read().decode()
        segs = [l for l in media.splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(segs), 2)                              # 3 x 100 s segments: the first is dropped
        self.assertEqual(sess.actual_start, 100.0)                  # 110 s falls in the second one, which stays
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", media)

    def test_no_start_means_the_whole_stream_and_a_known_zero(self):
        url = self.relay.add(self.origin + "/master.m3u8", {"Referer": REFERER}, "hls")
        self.assertEqual(self.relay.session_for(url).actual_start, 0.0)


class RewriteTests(unittest.TestCase):
    def test_drm_key_is_refused(self):
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:5,\na.ts\n'
        with self.assertRaises(DrmError):
            rewrite_hls(text, "https://x.com/p.m3u8", lambda u, r: u)

    def test_relative_urls_resolve_against_the_playlist(self):
        seen = []
        rewrite_hls("#EXTM3U\n#EXTINF:5,\n../a/seg.ts\n", "https://x.com/v/p.m3u8", lambda u, r: seen.append((u, r)) or "/x")
        self.assertEqual(seen, [("https://x.com/a/seg.ts", "segment")])

    def test_media_and_map_uris_are_rewritten(self):
        text = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="aud.m3u8"\n#EXT-X-MAP:URI="init.mp4"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n')
        roles = []
        rewrite_hls(text, "https://x.com/", lambda u, r: roles.append(r) or "/x")
        self.assertEqual(roles, ["playlist", "map", "playlist"])


class Misc(unittest.TestCase):
    def test_replay_headers_drops_browser_plumbing(self):
        kept = replay_headers({"Referer": "r", "Range": "bytes=0-", "Sec-Fetch-Mode": "cors", "Host": "h",
                               "Cookie": "a=b", ":authority": "x", "Accept-Encoding": "gzip"})
        self.assertEqual(kept, {"Referer": "r", "Cookie": "a=b"})

    def test_drm_sites_are_refused_by_name(self):
        self.assertIn("DRM", refusal_for("https://www.netflix.com/watch/1"))
        self.assertIsNone(refusal_for("https://some-anime-site.example/watch/1"))


class ProbeAll(unittest.TestCase):
    def stalled_server(self):
        """Accepts connections and never answers, like a stream host that has stopped responding."""
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        return srv.getsockname()[1]

    def test_a_stalled_server_cannot_hold_cast_past_the_budget(self):
        port = self.stalled_server()
        cands = [Candidate(url=f"http://127.0.0.1:{port}/ep{i}.m3u8", kind="hls") for i in range(5)]
        t0 = time.time()
        covered, drm = probe_all(cands, budget=0.5)
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual((covered, drm), ([], []))
        self.assertTrue(all(c.measured is False for c in cands))

    def test_a_stream_that_answers_is_measured_even_when_another_stalls(self):
        port = self.stalled_server()
        good = Candidate(url="http://good/x.m3u8", kind="hls")
        slow = Candidate(url=f"http://127.0.0.1:{port}/slow.m3u8", kind="hls")
        real = media_relay.probe_hls

        def probe(c):
            if c is not good:
                return real(c)
            c.duration = 2400.0
            return ["http://good/v720.m3u8"]

        with mock.patch("media_relay.probe_hls", probe):
            covered, _ = probe_all([good, slow], budget=0.5)
        self.assertEqual(covered, ["http://good/v720.m3u8"])
        self.assertTrue(good.measured)
        self.assertFalse(slow.measured)

    def test_drm_is_reported_and_a_refused_first_request_is_retried(self):
        locked = Candidate(url="http://a/locked.m3u8", kind="hls")
        shy = Candidate(url="http://a/shy.m3u8", kind="hls")
        tries = []

        def probe(c):
            if c is locked:
                raise DrmError("locked")
            tries.append(c)
            if len(tries) == 1:
                raise OSError("refused")
            return []

        with mock.patch("media_relay.probe_hls", probe):
            _, drm = probe_all([locked, shy], budget=3)
        self.assertEqual(len(drm), 1)
        self.assertIs(drm[0], locked)
        self.assertEqual(len(tries), 2)
        self.assertTrue(shy.measured)


if __name__ == "__main__":
    unittest.main()
