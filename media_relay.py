"""Relays a web video to the Roku.

Sites usually won't hand a video to a bare Roku: the stream needs the page's Referer, cookies or User-Agent. The
Roku asks this small server on the PC instead; the PC fetches from the site with the headers the browser used and
hands the bytes on. HLS playlists are rewritten so every segment and key also comes through here.

Safety: it binds to the PC's LAN address only, answers only the Roku's IP, and only knows URLs created by a cast
(random tokens), so it can't be used as an open proxy.
"""
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from drm import DrmError, key_line_is_drm
from stream_finder import parse_hls, parse_mpd
from upscaler import UpscaleSession, sweep
from yt_bridge import _Server

PORTS = range(8766, 8771)
MAX_SESSIONS = 6
MAX_PLAYLIST = 8_000_000
PROBE_BUDGET = 12.0     # seconds Cast will wait to measure streams before deciding with what it knows
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/156.0.0.0 Safari/537.36")
# request headers that must not be replayed (the relay sets its own or they describe the browser's connection)
DROP_HEADERS = {"host", "range", "accept-encoding", "connection", "content-length", "if-none-match",
                "if-modified-since", "if-range", "cache-control", "pragma", "te", "upgrade-insecure-requests"}
SEGMENT_EXT = (".ts", ".m4s", ".aac", ".m4a", ".mp4", ".mp3", ".vtt", ".webvtt", ".cmfv", ".cmfa")
URI_ATTR = re.compile(r'URI="([^"]*)"')


def replay_headers(headers):
    """The subset of a browser request's headers worth sending upstream."""
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in DROP_HEADERS or lk.startswith(("sec-", ":")):
            continue
        out[k] = v
    return out


def rewrite_hls(text, base_url, register):
    """Point every URL in an HLS playlist at the relay. register(abs_url, role) -> relay path.

    role is 'playlist', 'segment', 'key' or 'map'. Raises DrmError if the playlist asks for a DRM license."""
    out, next_is_playlist = [], False

    def swap(uri, role):
        if uri.startswith("data:"):
            return uri
        return register(urllib.parse.urljoin(base_url, uri), role)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            tag = line.split(":", 1)[0]
            if tag in ("#EXT-X-KEY", "#EXT-X-SESSION-KEY") and key_line_is_drm(line):
                raise DrmError("This stream's playlist asks for a DRM license, which CapoCast won't cast.")
            if tag == "#EXT-X-STREAM-INF":
                next_is_playlist = True
            if 'URI="' in line:
                role = ("playlist" if tag in ("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF")
                        else "key" if tag in ("#EXT-X-KEY", "#EXT-X-SESSION-KEY")
                        else "map" if tag == "#EXT-X-MAP" else "segment")
                line = URI_ATTR.sub(lambda m: f'URI="{swap(m.group(1), role)}"', line)
            out.append(line)
            continue
        is_playlist = next_is_playlist or urllib.parse.urlparse(line).path.lower().endswith(".m3u8")
        next_is_playlist = False
        out.append(swap(line, "playlist" if is_playlist else "segment"))
    return "\n".join(out) + "\n"


_HEADER_TAGS = ("#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-PLAYLIST-TYPE",
                "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-ALLOW-CACHE")


def trim_hls(text, start):
    """Make a finished (VOD) media playlist begin about `start` seconds in. Returns (playlist, seconds_skipped).

    The TV can't be told 'start at 12:40', but it will play whatever playlist it's given, so a seek is a playlist that
    begins at the right segment. The segment containing `start` is kept, so the real start is up to one segment
    earlier, and `seconds_skipped` says exactly where it is. Keys and init segments set earlier stay in force, and
    the media sequence number moves with the cut (AES-128 without an explicit IV derives its IV from it)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if start <= 0 or "#EXT-X-ENDLIST" not in text or not any(l.startswith("#EXTINF") for l in lines):
        return text, 0.0
    header, segs, pending, tail, cur = [], [], [], [], None
    for line in lines:
        if line.startswith("#EXT-X-ENDLIST"):
            tail.append(line)
        elif line.startswith("#EXTINF"):
            try:
                dur = float(line[8:].split(",")[0])
            except ValueError:
                dur = 0.0
            cur = {"lines": pending + [line], "dur": dur}
            pending = []
            segs.append(cur)
        elif cur is not None and not cur.get("done"):
            cur["lines"].append(line)              # BYTERANGE etc. and the segment's URI
            if not line.startswith("#"):
                cur["done"] = True
        elif line.startswith(_HEADER_TAGS) and not segs:
            header.append(line)
        else:
            pending.append(line)                   # KEY / MAP / DISCONTINUITY ahead of the next segment
        if cur is not None and cur.get("done") and not line.startswith("#"):
            cur = None
    elapsed, skip, carry = 0.0, 0, {}
    for seg in segs[:-1]:                          # never skip the last segment
        if elapsed + seg["dur"] > start:
            break
        elapsed += seg["dur"]
        skip += 1
        for l in seg["lines"]:
            if l.startswith("#EXT-X-KEY"):
                carry["key"] = l
            elif l.startswith("#EXT-X-MAP"):
                carry["map"] = l
    kept = segs[skip:]
    first = kept[0]["lines"]
    for name, tag in (("key", "#EXT-X-KEY"), ("map", "#EXT-X-MAP")):
        if name in carry and not any(l.startswith(tag) for l in first):
            first.insert(0, carry[name])
    if skip:
        seq = next((int(l.split(":")[1]) for l in header if l.startswith("#EXT-X-MEDIA-SEQUENCE")), 0) + skip
        header = [l for l in header if not l.startswith("#EXT-X-MEDIA-SEQUENCE")]
        header.append(f"#EXT-X-MEDIA-SEQUENCE:{seq}")
    out = header + [l for seg in kept for l in seg["lines"]] + tail
    return "\n".join(out) + "\n", elapsed


# URLs in a DASH manifest that name a segment or a base: <BaseURL>, and media= / initialization= templates
_MPD_BASE = re.compile(r"(<BaseURL[^>]*>)\s*(https?://[^/<\s]+/)([^<]*)(</BaseURL>)")
_MPD_ATTR = re.compile(r'\b(media|initialization|sourceURL|index|mediaURL)="(https?://[^/"]+/)([^"]*)"')


def rewrite_mpd(text, register_base):
    """Point a DASH manifest's absolute URLs at the relay. register_base(scheme://host/) -> '/s/<token>/bN/'.
    Relative URLs are left alone: they resolve against the manifest, which is served from the relay."""
    text = _MPD_BASE.sub(lambda m: f"{m.group(1)}{register_base(m.group(2))}{m.group(3)}{m.group(4)}", text)
    return _MPD_ATTR.sub(lambda m: f'{m.group(1)}="{register_base(m.group(2))}{m.group(3)}"', text)


def _ext_for(url, role):
    ext = "." + urllib.parse.urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urllib.parse.urlparse(url).path else ""
    if role == "playlist":
        return ".m3u8"
    if role == "key":
        return ".key"
    if role == "map":
        return ext if ext in SEGMENT_EXT else ".mp4"
    return ext if ext in SEGMENT_EXT else ".ts"


class Session:
    """One cast: the entry URL, the headers to replay, and every sub-resource a playlist has pointed at."""

    def __init__(self, url, headers, kind, token, start=0.0):
        self.token, self.kind, self.headers = token, kind, {**{"User-Agent": UA}, **replay_headers(headers)}
        # HLS only: begin this many seconds in. The TV then reports its position from the cut, so actual_start (set
        # when the trimmed playlist is first served) is what to add to the TV's position to get the video's time.
        self.start, self.actual_start = float(start or 0.0), (None if start else 0.0)
        self.entry_name = {"hls": "index.m3u8", "dash": "manifest.mpd"}.get(kind, "video.mp4")
        self._urls = {self.entry_name: url}
        self._names = {url: self.entry_name}
        self._lock = threading.Lock()
        self.created, self.hits, self.error = time.time(), 0, ""
        self.failures = []                  # upstream requests that failed, e.g. [(403, 'seg12.ts')]: a stream going bad
        # DASH only: segment URLs are templates, so they can't be registered one by one. They're resolved against
        # the manifest's address, or against a base the manifest itself declared; nothing else is ever fetched.
        self.mpd_url = url if kind == "dash" else None
        self.bases = {}                                   # 'b1' -> 'https://cdn.example/'
        self._hosts = {urllib.parse.urlparse(url).netloc}

    def register_base(self, base):
        """A host the manifest names (e.g. its CDN). Returns the relay path prefix that stands for it."""
        with self._lock:
            for name, b in self.bases.items():
                if b == base:
                    return f"/s/{self.token}/{name}/"
            name = f"b{len(self.bases) + 1}"
            self.bases[name] = base
            self._hosts.add(urllib.parse.urlparse(base).netloc)
        return f"/s/{self.token}/{name}/"

    def register(self, abs_url, role):
        with self._lock:
            name = self._names.get(abs_url)
            if name is None:
                name = f"r{len(self._urls)}{_ext_for(abs_url, role)}"
                self._urls[name], self._names[abs_url] = abs_url, name
        return f"/s/{self.token}/{name}"

    def url_for(self, name):
        with self._lock:
            known = self._urls.get(name)
        if known or self.kind != "dash":
            return known
        # DASH: 'b1/path' is a declared base; anything else is relative to the manifest
        first, _, rest = name.partition("/")
        if first in self.bases:
            target = urllib.parse.urljoin(self.bases[first], rest)
        else:
            target = urllib.parse.urljoin(self.mpd_url, name)
        host = urllib.parse.urlparse(target).netloc
        if urllib.parse.urlparse(target).scheme not in ("http", "https") or host not in self._hosts:
            return None                                   # never an open proxy: only the manifest's own hosts
        return target


class MediaRelay:
    def __init__(self, ip, allow, log=lambda m: None, allow_loopback=False):
        self.ip, self.allow, self.log, self.allow_loopback = ip, allow, log, allow_loopback
        self.port = 0
        self.sessions = {}
        self._lock = threading.Lock()
        self.server = None

    @property
    def base(self):
        return f"http://{self.ip}:{self.port}"

    def start(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "RokuListen"

            def log_message(self, *a):
                pass

            def do_GET(self):
                relay._serve(self, head=False)

            def do_HEAD(self):
                relay._serve(self, head=True)

        last = None
        for port in PORTS:
            try:
                self.server = _Server((self.ip, port), Handler)
                self.port = port
                break
            except OSError as e:
                last = e
        if not self.server:
            raise OSError(f"ports {PORTS.start}-{PORTS.stop - 1} are all busy ({last})")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log(f"media relay listening on {self.base}")
        sweep()

    def stop(self):
        self.stop_upscaling()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def add(self, url, headers, kind, start=0.0):
        """Register a stream; returns the URL to give the Roku. HLS can begin `start` seconds in."""
        return self._keep(Session(url, headers, kind, secrets.token_hex(8), start if kind == "hls" else 0.0))

    def add_upscaled(self, url, headers, kind, start, duration, mode, audio, subs=None, hdr=False):
        """Like add(), but the TV gets a copy made on this PC: upscaled to 4K, with subtitles (a file at `subs`) burned
        in, with simulated HDR (`hdr`), or any of them. Any kind of stream can begin `start` seconds in. One encoder at a time: the GPU has one TV to feed,
        so this replaces any upscale already running."""
        self.stop_upscaling()
        sent = {**{"User-Agent": UA}, **replay_headers(headers)}
        return self._keep(UpscaleSession(url, sent, kind, secrets.token_hex(8), start, duration, mode, audio, self.log,
                                     subs, hdr))

    def stop_upscaling(self):
        with self._lock:
            running = [s for s in self.sessions.values() if isinstance(s, UpscaleSession)]
            for s in running:
                self.sessions.pop(s.token, None)
        for s in running:
            s.close()

    def _keep(self, sess):
        with self._lock:
            self.sessions[sess.token] = sess
            for old in sorted(self.sessions.values(), key=lambda s: s.created)[:-MAX_SESSIONS]:
                self.sessions.pop(old.token, None)
                if isinstance(old, UpscaleSession):
                    old.close()
        return f"{self.base}/s/{sess.token}/{sess.entry_name}"

    def session_for(self, public_url):
        m = re.search(r"/s/([0-9a-f]+)/", public_url)
        return self.sessions.get(m.group(1)) if m else None

    # ---- request handling ----------------------------------------------------
    def _client_ok(self, ip):
        return ip == self.allow() or (self.allow_loopback and ip == "127.0.0.1")

    def _serve(self, h, head):
        if not self._client_ok(h.client_address[0]):
            return self._plain(h, 403, head)
        req = urllib.parse.urlparse(h.path)
        parts = req.path.split("/")                                   # ['', 's', token, name, ...]
        sess = self.sessions.get(parts[2]) if len(parts) >= 4 and parts[1] == "s" else None
        if isinstance(sess, UpscaleSession):
            return self._serve_upscaled(h, sess, parts[3], head) if len(parts) == 4 else self._plain(h, 404, head)
        if sess and (sess.kind == "dash" or len(parts) == 4):
            name = "/".join(parts[3:]) + (f"?{req.query}" if req.query and sess.kind == "dash" else "")
            upstream = sess.url_for(urllib.parse.unquote(name) if sess.kind == "dash" else parts[3])
        else:
            upstream = None
        if not upstream:
            return self._plain(h, 404, head)
        name = parts[-1]
        parts = ["", "s", parts[2], name]                             # the handlers below only look at the file name
        sess.hits += 1
        headers = dict(sess.headers)
        if h.headers.get("Range"):
            headers["Range"] = h.headers["Range"]
        try:
            resp = urllib.request.urlopen(urllib.request.Request(upstream, headers=headers), timeout=20)
        except urllib.error.HTTPError as e:
            self.log(f"relay: upstream {e.code} for {upstream[:120]}")
            sess.failures.append((e.code, parts[3]))
            return self._plain(h, e.code if e.code in (403, 404, 416) else 502, head)
        except Exception as e:
            self.log(f"relay: upstream failed for {upstream[:120]}: {e!r}")
            sess.failures.append((0, parts[3]))
            return self._plain(h, 502, head)
        try:
            if sess.kind == "dash" and parts[3] == "manifest.mpd":
                self._manifest(h, sess, resp, head)
            elif parts[3].endswith(".m3u8"):
                self._playlist(h, sess, resp, head)
            else:
                self._stream(h, resp, parts[3], head)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass                                     # the Roku hung up (seek, channel closed): normal
        finally:
            resp.close()

    def _serve_upscaled(self, h, sess, name, head):
        """The TV's requests for an upscaled cast are answered from this PC; the site is the encoder's business."""
        sess.hits += 1
        text = sess.playlist(name)
        try:
            if text is not None:
                if sess.start > 0 and "#EXTINF" in text:          # the same trim a plain HLS cast seeks with
                    text, skipped = trim_hls(text, sess.start)
                    if sess.actual_start is None:
                        sess.actual_start = skipped
                body = text.encode()
                h.send_response(200)
                h.send_header("Content-Type", "application/vnd.apple.mpegurl")
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                if not head:
                    h.wfile.write(body)
                return
            path = sess.file_for(name)
            if not path:
                sess.failures.append((0, name))
                return self._plain(h, 503, head)
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                h.send_response(200)
                h.send_header("Content-Type", "video/mp4")
                h.send_header("Content-Length", str(size))
                h.end_headers()
                if not head:
                    began = time.time()
                    shutil.copyfileobj(f, h.wfile, 65536)
                    if name.startswith("v") and name.endswith(".m4s"):
                        sess.sent(size, time.time() - began)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass                                     # the Roku hung up (seek, channel closed): normal

    @staticmethod
    def _plain(h, code, head):
        body = b"" if head else str(code).encode()
        h.send_response(code)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _playlist(self, h, sess, resp, head):
        raw = resp.read(MAX_PLAYLIST)
        text = raw.decode("utf-8", errors="replace")
        if sess.start > 0 and "#EXTINF" in text:                  # a media playlist (not a master): seek by trimming
            text, skipped = trim_hls(text, sess.start)
            if sess.actual_start is None:
                sess.actual_start = skipped
        try:
            body = rewrite_hls(text, resp.geturl(), sess.register).encode()
        except DrmError as e:
            sess.error = str(e)
            return self._plain(h, 451, head)
        h.send_response(200)
        h.send_header("Content-Type", "application/vnd.apple.mpegurl")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if not head:
            h.wfile.write(body)

    def _manifest(self, h, sess, resp, head):
        text = resp.read(MAX_PLAYLIST).decode("utf-8", errors="replace")
        if parse_mpd(text)["drm"]:
            sess.error = "This stream asks for a DRM license, which CapoCast won't cast."
            return self._plain(h, 451, head)
        sess.mpd_url = resp.geturl()                      # where relative segment paths are relative to
        sess._hosts.add(urllib.parse.urlparse(sess.mpd_url).netloc)
        body = rewrite_mpd(text, sess.register_base).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/dash+xml")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if not head:
            h.wfile.write(body)

    @staticmethod
    def _stream(h, resp, name, head):
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        if ctype.startswith(("image/", "text/")) and name.endswith(".ts"):
            ctype = "video/mp2t"                     # some CDNs disguise segments as pictures
        h.send_response(resp.status)
        h.send_header("Content-Type", ctype)
        length = resp.headers.get("Content-Length")
        if length:
            h.send_header("Content-Length", length)
        else:
            h.send_header("Connection", "close")
            h.close_connection = True
        for k in ("Content-Range", "Accept-Ranges"):
            if resp.headers.get(k):
                h.send_header(k, resp.headers[k])
        if name.endswith(".mp4") and not resp.headers.get("Accept-Ranges"):
            h.send_header("Accept-Ranges", "bytes")
        h.end_headers()
        if head:
            return
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            h.wfile.write(chunk)


def fetch_text(url, headers, timeout=15):
    req = urllib.request.Request(url, headers={**{"User-Agent": UA}, **replay_headers(headers)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_PLAYLIST).decode("utf-8", errors="replace"), resp.geturl()


def _tracks(data):
    """Which kinds of track (video / audio) are in a chunk of media, via ffprobe. None if it can't tell."""
    exe = shutil.which("ffprobe")
    if not exe or not data:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", "-"],
                             input=data, capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.decode(errors="replace")
    except Exception:
        return None
    kinds = {l.strip().split(",")[0] for l in out.splitlines() if l.strip()}
    return kinds or None


def inspect_media_playlist(cand, text, base_url):
    """Look inside the first piece of a media playlist to learn whether it has picture and sound, and note it on cand.

    A site may serve the video and the audio as separate playlists; picking the wrong one casts silence (or a black
    screen). The playlist alone doesn't say, so fetch the first segment (with its init segment, for fMP4)."""
    uris, init = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP") and 'URI="' in line:
            init = urllib.parse.urljoin(base_url, line.split('URI="')[1].split('"')[0])
        elif line and not line.startswith("#"):
            uris.append(urllib.parse.urljoin(base_url, line))
            break
    if not uris or "#EXT-X-KEY:METHOD=AES-128" in text:
        return                                       # encrypted segments can't be looked into; leave unknown
    data = b""
    for u in ([init] if init else []) + [uris[0]]:
        req = urllib.request.Request(u, headers={**{"User-Agent": UA}, **replay_headers(cand.headers)})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data += resp.read(400_000)
    kinds = _tracks(data)
    if kinds:
        cand.has_video, cand.has_audio = "video" in kinds, "audio" in kinds


def probe_dash(cand):
    """Read a DASH manifest to learn its length and height. Raises DrmError for DRM. Returns [] (no variants)."""
    text, _ = fetch_text(cand.url, cand.headers)
    info = parse_mpd(text)
    if info["drm"]:
        raise DrmError("This stream is DRM-protected, which CapoCast won't cast.")
    if not info["valid"]:
        raise ValueError("not a manifest")
    cand.height, cand.width = info["height"], info["width"]
    cand.duration, cand.live, cand.master = info["duration"], info["live"], True
    cand.has_audio, cand.has_video = info["audio"] or None, info["video"] or None
    return []


def probe_hls(cand):
    """Read the playlist(s) behind a candidate to learn its length and height. Raises DrmError for DRM.

    Returns the URLs of the variant playlists (so the caller can drop them as duplicates of this master)."""
    text, final = fetch_text(cand.url, cand.headers)
    info = parse_hls(text, final)
    if info["drm"]:
        raise DrmError("This stream is DRM-protected, which CapoCast won't cast.")
    if not info["valid"]:
        raise ValueError("not a playlist")
    cand.master, cand.height, cand.width, covered = info["master"], info["height"], info["width"], []
    cand.has_audio, cand.has_video = info["audio"], info["video"]
    if info["master"] and info["variants"]:
        covered = [v["url"] for v in info["variants"]]
        text2, final2 = fetch_text(info["variants"][0]["url"], cand.headers)
        sub = parse_hls(text2, final2)
        if sub["drm"]:
            raise DrmError("This stream is DRM-protected, which CapoCast won't cast.")
        cand.duration, cand.live = sub["duration"], sub["live"]
        if cand.has_audio is None or cand.has_video is None:        # the master didn't say: look in the media
            try:
                inspect_media_playlist(cand, text2, final2)
            except Exception:
                pass
    else:
        cand.duration, cand.live = info["duration"], info["live"]
        try:
            inspect_media_playlist(cand, text, final)
        except Exception:
            pass
    return covered


def probe_all(cands, log=lambda m: None, budget=PROBE_BUDGET):
    """Measure streams side by side, waiting at most `budget` seconds. One read from a stalled server takes 15 s to
    fail, so checked in turn a few of them hold the Cast button for minutes.

    Returns (variant URLs, DRM-protected candidates). A stream still unread at the deadline gets measured=False."""
    covered, drm = [], []

    def one(c):
        probe = probe_hls if c.kind == "hls" else probe_dash
        for last_try in (False, True):               # some CDNs refuse a first request
            try:
                covered.extend(probe(c))
                return
            except DrmError:
                drm.append(c)
                return
            except Exception as e:
                log(f"probe failed for {c.url[:100]}: {e!r}")
                if not last_try:
                    time.sleep(0.6)
        c.measured = False                           # keep it (it may still play) but rank it below streams we read

    workers = [(c, threading.Thread(target=one, args=(c,), name="stream-probe", daemon=True)) for c in cands]
    for _, t in workers:
        t.start()
    end = time.time() + budget
    for c, t in workers:
        t.join(max(0.0, end - time.time()))
        if t.is_alive() and not c.duration:
            c.measured = False
            log(f"probe gave up waiting for {c.url[:100]}")
    return list(covered), list(drm)
