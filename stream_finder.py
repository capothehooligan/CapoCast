"""Pick the real video out of everything a page loads. Pure logic, no network and no Tk, so it can be tested.

A sketchy streaming page loads a lot of video-ish things: ad clips, previews, thumbnails, loose segments and,
somewhere in there, the episode. This module throws out what can't be the episode and ranks what's left.
"""
import re
import urllib.parse
from dataclasses import dataclass, field

MIN_SECONDS = 90        # anything shorter is an ad, a preview or a teaser
MIN_MP4_BYTES = 4_000_000   # an mp4 of unknown length smaller than this is a short clip

HLS_MIMES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}
MP4_MIMES = {"video/mp4", "video/x-m4v", "video/quicktime"}
DASH_MIMES = {"application/dash+xml"}
UNSUPPORTED_MIMES = {"video/webm", "video/x-matroska", "video/x-flv"}
UNSUPPORTED_EXT = (".webm", ".mkv", ".flv", ".avi", ".wmv")
# Content types a disguised playlist or manifest is often served with; the body is checked for the real thing.
SNIFF_MIMES = {"text/plain", "application/octet-stream", "binary/octet-stream", "application/binary", "text/xml",
               "application/xml", "text/html", "application/json", "image/png", "image/jpeg", "image/gif"}
# pieces of a stream (or page furniture), never a stream by themselves
PIECE_EXT = (".ts", ".m4s", ".aac", ".m4a", ".cmfv", ".cmfa", ".mp3", ".vtt", ".webvtt", ".srt", ".jpg", ".jpeg",
             ".png", ".gif", ".webp", ".svg", ".js", ".css", ".key", ".json")

AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "imasdk.googleapis.com", "adnxs.com",
    "adsrvr.org", "pubmatic.com", "rubiconproject.com", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "popads.net", "popcash.net", "propellerads.com", "exoclick.com", "juicyads.com", "trafficjunky.net",
    "adsterra.com", "hilltopads.net", "clickadu.com", "mgid.com", "revcontent.com", "smartadserver.com",
    "openx.net", "adform.net", "teads.tv", "spotxchange.com", "springserve.com", "tremorhub.com", "yieldmo.com",
)
AD_HINTS = ("/ads/", "/ad/", "adserver", "preroll", "pre-roll", "midroll", "mid-roll", "/vast", "advert", "adtech",
            "/promo/")
SOFT_JUNK = ("thumb", "sprite", "storyboard", "preview", "poster", "teaser", "trailer", "intro_ad")
# a number straight after one of these is a whole title's id, not an episode
TITLE_SEGMENTS = {"watch", "movie", "movies", "film", "films", "title", "titles", "tv", "show", "shows", "series",
                  "video", "videos", "v", "anime", "play", "stream", "id"}
TITLE_KEYS = {"id", "v", "vid", "video", "title", "movie"}


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def site_of(host):
    """Registrable-ish domain: 'v.cdn.example.co.uk' -> 'example.co.uk'. Good enough to tell sites apart."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2 or re.fullmatch(r"[\d.]+", host):
        return host
    if len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "ne", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_show(a, b):
    """True if two page addresses look like neighbouring episodes of one show, not two different titles.

    Only numbers may differ ('?s=1&e=5', '/naruto-episode-5'). A bare id after a generic word ('/movie/1507696') or a
    key like ?id= names a whole title, so a change there doesn't count."""
    ua, ub = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    if not ua.hostname or site_of(ua.hostname) != site_of(ub.hostname):
        return False
    pa, pb = ua.path.strip("/").split("/"), ub.path.strip("/").split("/")
    qa, qb = dict(urllib.parse.parse_qsl(ua.query)), dict(urllib.parse.parse_qsl(ub.query))
    if len(pa) != len(pb) or qa.keys() != qb.keys():
        return False
    moved = False
    for i, (x, y) in enumerate(zip(pa, pb)):
        if x == y:
            continue
        if re.sub(r"\d+", "#", x) != re.sub(r"\d+", "#", y):
            return False
        if x.isdigit() and (i == 0 or pa[i - 1].lower() in TITLE_SEGMENTS):
            return False
        moved = True
    for k, x in qa.items():
        if x == qb[k]:
            continue
        if k.lower() in TITLE_KEYS or not (x.isdigit() and qb[k].isdigit()):
            return False
        moved = True
    return moved


def is_ad_url(url):
    host = host_of(url)
    if any(host == h or host.endswith("." + h) for h in AD_HOSTS):
        return True
    path = urllib.parse.urlparse(url).path.lower()
    return any(h in path for h in AD_HINTS)


def sniff(text):
    """'hls' | 'dash' | None: what a response body really is, whatever its URL and content type claim."""
    head = text[:2048].lstrip("﻿ \t\r\n")
    if head.startswith("#EXTM3U"):
        return "hls"
    if head.startswith(("<?xml", "<MPD")) and "<MPD" in text[:8192]:
        return "dash"
    return None


def classify(url, mime=""):
    """'hls' | 'dash' | 'mp4' | 'unsupported' | None (not a whole video)."""
    path = urllib.parse.urlparse(url).path.lower()
    m = (mime or "").split(";")[0].strip().lower()
    if path.endswith(PIECE_EXT):
        return None
    if path.endswith(UNSUPPORTED_EXT) or m in UNSUPPORTED_MIMES:
        return "unsupported"
    if path.endswith(".mpd") or m in DASH_MIMES:
        return "dash"
    if path.endswith(".m3u8") or m in HLS_MIMES:
        return "hls"
    if path.endswith((".mp4", ".m4v", ".mov")) or m in MP4_MIMES:
        return "mp4"
    return None


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line):
    return {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[-1])}


def parse_hls(text, base_url=""):
    """Facts about a playlist: master or media, length, live, tallest rendition and its variant URLs."""
    from drm import key_line_is_drm
    info = {"valid": text.lstrip().startswith("#EXTM3U"), "master": False, "duration": 0.0, "live": False,
            "height": 0, "width": 0, "variants": [], "drm": False, "audio": None, "video": None}
    codecs, audio_group = [], False
    ended, want_uri, pending = False, False, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:") and _attrs(line).get("TYPE") == "AUDIO":
            audio_group = True                       # the sound is a separate rendition the player combines
        if line.startswith("#EXT-X-STREAM-INF"):
            info["master"], want_uri, pending = True, True, _attrs(line)
            codecs.append(pending.get("CODECS", ""))
        elif line.startswith("#EXTINF:"):
            try:
                info["duration"] += float(line[8:].split(",")[0])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")) and key_line_is_drm(line):
            info["drm"] = True
        elif not line.startswith("#") and want_uri:
            want_uri = False
            height = width = 0
            m = re.match(r"(\d+)x(\d+)", pending.get("RESOLUTION", ""))
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            info["height"] = max(info["height"], height)
            info["width"] = max(info["width"], width)
            info["variants"].append({"url": urllib.parse.urljoin(base_url, line), "height": height,
                                     "bandwidth": int(pending.get("BANDWIDTH", "0") or 0)})
    info["variants"].sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
    if info["master"]:
        joined = " ".join(codecs).lower()
        if audio_group or re.search(r"mp4a|ac-3|ec-3|opus|flac", joined):
            info["audio"] = True
        elif joined.strip():
            info["audio"] = False                    # codecs are listed and none is audio
        if info["height"] or re.search(r"avc1|hvc1|hev1|av01|vp09", joined):
            info["video"] = True
    info["live"] = bool(info["valid"] and not info["master"] and not ended)
    return info


def _iso_seconds(text):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", (text or "").strip())
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_mpd(text):
    """Facts about a DASH manifest: length, live, tallest rendition, and whether it needs a DRM license."""
    import xml.etree.ElementTree as ET
    info = {"valid": False, "duration": 0.0, "live": False, "height": 0, "width": 0, "drm": False, "audio": False,
            "video": False}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return info
    if not root.tag.endswith("MPD"):
        return info
    info["valid"] = True
    info["live"] = root.get("type") == "dynamic"
    info["duration"] = _iso_seconds(root.get("mediaPresentationDuration"))
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("AdaptationSet", "Representation"):
            kind = (el.get("mimeType") or el.get("contentType") or "").lower()
            info["audio"] = info["audio"] or kind.startswith("audio")
            info["video"] = info["video"] or kind.startswith("video")
        if tag in ("Representation", "AdaptationSet") and el.get("height", "").isdigit():
            info["height"] = max(info["height"], int(el.get("height")))
        if tag in ("Representation", "AdaptationSet") and el.get("width", "").isdigit():
            info["width"] = max(info["width"], int(el.get("width")))
        if tag == "ContentProtection":
            scheme = (el.get("schemeIdUri") or "").lower()
            if scheme.startswith("urn:uuid:") and "e2719d58-a985-b3c9-781a-b030af78d30e" not in scheme:
                info["drm"] = True       # Widevine, PlayReady, FairPlay... (plain 'cenc' declarations aren't DRM)
    return info


@dataclass
class Candidate:
    url: str
    kind: str                       # 'hls' | 'dash' | 'mp4'
    headers: dict = field(default_factory=dict)   # what the browser sent, replayed by the relay
    frame: str = ""                 # id of the frame that asked for it
    size: int = 0                   # total bytes (mp4), 0 if unknown
    duration: float = 0.0           # seconds, 0 if unknown
    height: int = 0
    width: int = 0                  # of the biggest rendition, 0 if unknown
    master: bool = False
    live: bool = False
    seen: int = 0                   # arrival order; a later request beats an earlier one (ads come first)
    active_age: float = 1e9         # seconds since the page last downloaded video pieces from this stream's server
    measured: bool = True           # False if its playlist couldn't be read (length and quality unknown)
    has_audio: object = None        # True / False once checked, None if unknown
    has_video: object = None
    on_page: bool = False           # the page's <video> is playing exactly this URL
    covered: bool = False           # a variant of a master playlist we already have
    stale: bool = False             # seen before the page changed its address without reloading

    def label(self):
        bits = [self.kind.upper()]
        if self.height:
            bits.append(f"{self.height}p")
        if self.live:
            bits.append("live")
        elif self.duration:
            s = int(self.duration)
            bits.append(f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}")
        elif self.size:
            bits.append(f"{self.size / 1e6:.0f} MB")
        return "  ·  ".join(bits) + f"   {host_of(self.url)}"


def apply_page_videos(cands, videos):
    """Fill in what the page's own <video> elements say: their URL, length and whether they're playing."""
    for v in videos:
        for c in cands:
            if v.get("src") and v["src"] == c.url:
                c.on_page = True
                c.duration = c.duration or float(v.get("dur") or 0)


def reject_reason(c):
    """Why this can't be the episode, or None if it still might be."""
    if c.covered:
        return "variant"
    if is_ad_url(c.url):
        return "ad"
    if c.has_video is False:
        return "audio only"                          # the sound track on its own: no picture
    if c.live:
        return None
    if c.duration and c.duration < MIN_SECONDS:
        return "short"
    if not c.duration and c.kind == "mp4" and c.size and c.size < MIN_MP4_BYTES:
        return "small"
    return None


def score(c, active_frames=()):
    s = 0.0
    if c.duration:
        s += min(c.duration, 7200) / 60          # up to 120
    if c.live:
        s += 40
    if c.master:
        s += 30
    if c.kind == "mp4" and c.size:
        s += min(c.size / 1e6, 2000) / 20        # up to 100
    if c.height:
        s += min(c.height, 1080) / 36            # up to 30
    if c.frame and c.frame in active_frames:
        s += 200                                  # it came from the frame that's playing the biggest video
    if c.on_page:
        s += 150
    if c.active_age < 25:
        s += 250 - c.active_age * 6       # the player is pulling video from this server right now: it's the one playing
    if c.has_audio is False:
        s -= 400                                     # a picture with no sound: only if there's nothing else
    elif c.has_audio:
        s += 25
    if not c.measured and c.kind != "mp4":
        s -= 70                           # a playlist we couldn't even read is a poor bet
    low = c.url.lower()
    if any(h in low for h in SOFT_JUNK):
        s -= 80
    return s + c.seen * 0.01


@dataclass
class Decision:
    best: Candidate = None
    close: list = field(default_factory=list)   # other plausible answers; non-empty means "ask the user"
    rejected: int = 0
    unsupported: bool = False
    playing: bool = False                       # the best one is being downloaded by the player right now


def choose(cands, active_frames=(), unsupported=False):
    """Rank the survivors. If two are nearly as good as each other, return them all so the user can pick."""
    keep = [c for c in cands if reject_reason(c) is None]
    rejected = len(cands) - len(keep)
    if not keep:
        return Decision(None, [], rejected, unsupported)
    ranked = sorted(keep, key=lambda c: score(c, active_frames), reverse=True)
    top = score(ranked[0], active_frames)
    best = ranked[0]
    # The same video offered several times (master + variants, mirror servers, a retry) isn't a choice: only streams
    # of a clearly different length are real alternatives. Unknown lengths can't be called the same.
    same = lambda c: bool(c.duration and best.duration and abs(c.duration - best.duration) <= 3)
    close = [c for c in ranked[1:4] if score(c, active_frames) >= top * 0.75 and c.url != best.url and not same(c)]
    return Decision(ranked[0], close, rejected, unsupported, playing=ranked[0].active_age < 25)
