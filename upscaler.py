"""Upscales a web video to 4K for the TV, with the Anime4K shaders for animation or FSRCNNX for live action.

The Roku can't run shaders, so the PC does the work and the TV just plays the result: FFmpeg reads the site's stream,
libplacebo runs the shader on the GPU (Vulkan), NVENC encodes HEVC, and media_relay serves the pieces to the TV.

The TV is given a finished (VOD) playlist for the whole video before most of it exists, and pieces are made as it
asks for them. That keeps everything the plain cast relies on working: a page seek is still a trimmed playlist, and
the TV's own Left / Right jump just asks for a piece further on, which restarts the encoder there. Every piece is
SEGMENT seconds of the video's own timeline, so a piece made by a restarted encoder fits the playlist exactly.

Video and audio are separate tracks in fragmented MP4 (CMAF), the form Roku's streaming spec lists for HEVC over HLS.

The same pipeline also burns subtitles into the picture: with `subs` the words are drawn onto each frame (after the
upscale, so they stay sharp), and the "burn" mode does only that, at the video's own size, for casts that aren't being
upscaled. The TV can't show a separate subtitle file, so this is how it gets any.
"""
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SEGMENT = 4             # seconds per piece; Roku recommends 4-6
TARGET_W, TARGET_H = 3840, 2160
MAX_BITRATE = 16_000_000
GAIN_NEEDED = 1.3       # the shaders only run when the picture grows by more than this (their own //!WHEN rule), so
                        # a source within 1.3x of 4K is just re-encoded: no detail added, a startup delay for nothing
AUDIO_BITRATE = 192_000
READ_RATE, BURST = 2.0, 60      # a 60 s head start, then twice real time. The TV plays smoothly once the encoder is
                                # well ahead of it, so get there fast; an RTX 5060 manages 2.3x on HQ at 1080p
AHEAD = 75              # pieces (5 min) to stay ahead of the TV. A paused or departed TV stops the encoder here...
REFILL = 15             # ...and it starts again when the TV is this close to the end of what's been made
KEEP_BEHIND = 8         # pieces kept behind the TV, for a short jump back
JUMP = 6                # a request this far past the encoder is the TV's read-ahead, to be waited for. Further is a seek
WAIT = 25.0             # seconds the TV's request may wait for a piece
SENT_EVERY = 8          # video pieces between notes in the log of how fast the TV is taking them
OLD_DIRS = 86400        # a leftover work folder older than this came from a run that crashed
BURN = "burn"           # subtitles only: no shader, the video's own size
MODES = {"fast": "anime4k/anime4k_fast.glsl", "hq": "anime4k/anime4k_hq.glsl", "movie": "fsrcnnx/FSRCNNX_x2_16-0-4-1.glsl"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
QUIET_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
DIR_PREFIX = "capocast-upscale-"
PIECE = re.compile(r"^([va])(\d{5})\.m4s$")
# Simulated HDR. FFmpeg's own inverse tone mapping aims for PQ's full 10,000 nits (white really does land there), so the
# curve is ours: everything up to the knee keeps its SDR brightness, and only the highlights above it rise to the peak.
HDR_WHITE = 203         # nits for SDR white, the reference level in BT.2408
HDR_PEAK = 500          # nits for the brightest thing in the picture. The number to tune by eye on the TV
HDR_KNEE = 0.5          # the share of SDR's linear light left alone
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")   # a source with one of these is real HDR already
HDR_TAGS = "color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"


def already_4k(width, height):
    """True if upscaling can't add anything: the picture is at least 2160 tall, or - letterboxed films are 3840 wide
    but only about 1600 tall - it would grow by 1.3x or less when fitted into 4K."""
    if height >= TARGET_H:
        return True
    return bool(width and height) and min(TARGET_W / width, TARGET_H / height) <= GAIN_NEEDED


def hdr_shader():
    """A libplacebo hook that brightens the highlights. It runs on linear light, where 1.0 is SDR white, and the
    brightest of the three channels sets the gain for all three, so a colour gets brighter without changing hue.
    libplacebo does the rest: BT.709 to BT.2020 and the PQ curve."""
    return f"""//!HOOK LINEAR
//!BIND HOOKED
//!DESC simulated HDR
const float WHITE = {HDR_WHITE:.1f};
const float PEAK = {HDR_PEAK:.1f};
const float KNEE = {HDR_KNEE:.3f};
vec4 hook() {{
    vec4 c = HOOKED_texOff(vec2(0.0));
    vec3 rgb = clamp(c.rgb, 0.0, 1.0);    // a scaler overshoots SDR's range at sharp edges: ringing, not light
    float top = max(max(rgb.r, rgb.g), rgb.b);
    if (top <= KNEE) return vec4(rgb, c.a);
    float t = (top - KNEE) / (1.0 - KNEE);
    float nits = WHITE * top + (PEAK - WHITE) * t * t;
    return vec4(rgb * (nits / WHITE / top), c.a);
}}
"""


def font_source():
    """The folder holding the fonts subtitles are drawn in (the same ones the app's own text uses)."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "assets")


def shader_path(mode):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", *MODES[mode].split("/"))


def missing(mode="fast", subs=False, hdr=False):
    """Why upscaling (or, with mode='burn', adding subtitles and/or HDR) can't run on this PC, in plain words, or ''
    if it can."""
    what = ("Subtitles need" if subs else "HDR needs") if mode == BURN else "Upscaling needs"
    exe = shutil.which("ffmpeg")
    if not exe:
        return f"{what} FFmpeg on your PATH."

    def listing(flag):
        try:
            return subprocess.run([exe, "-hide_banner", flag], capture_output=True, timeout=10,
                                  creationflags=NO_WINDOW).stdout.decode(errors="replace")
        except Exception:
            return ""

    filters = listing("-filters")
    if (mode != BURN or hdr) and "libplacebo" not in filters:
        return "This FFmpeg has no libplacebo. Install the full build: winget install Gyan.FFmpeg"
    if (subs or mode == BURN) and " subtitles " not in filters:
        return "This FFmpeg can't draw subtitles. Install the full build: winget install Gyan.FFmpeg"
    if "hevc_nvenc" not in listing("-encoders"):
        return "This FFmpeg has no NVIDIA encoder. Install the full build: winget install Gyan.FFmpeg"
    return ""


def probe(url, headers, kind, log=lambda m: None):
    """What FFmpeg itself sees in a stream: {'duration', 'height', 'width', 'transfer', 'audio', 'video'}, or None if
    it can't read it."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", *_input_options(headers, kind), "-show_entries",
                              "stream=codec_type,width,height,color_transfer:format=duration", "-of", "default=nw=1", url],
                             capture_output=True, timeout=25, creationflags=NO_WINDOW).stdout.decode(errors="replace")
    except Exception as e:
        log(f"upscale probe failed: {e!r}")
        return None
    info = {"duration": 0.0, "height": 0, "width": 0, "transfer": "", "audio": False, "video": False}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "codec_type" and value in ("audio", "video"):
            info[value] = True
        elif key == "height" and value.isdigit():
            info["height"] = max(info["height"], int(value))
        elif key == "width" and value.isdigit():
            info["width"] = max(info["width"], int(value))
        elif key == "color_transfer" and value != "unknown":
            info["transfer"] = value
        elif key == "duration":
            try:
                info["duration"] = float(value)
            except ValueError:
                pass
    return info if info["video"] or info["audio"] else None


def _input_options(headers, kind):
    """FFmpeg's options for fetching a stream the way the browser did. Sites' cookies pass through here, so nothing
    built from this is ever logged."""
    disguised = ["-allowed_extensions", "ALL", "-extension_picky", "0"] if kind == "hls" else []   # e.g. segN.jpg
    agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), "")
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
    return [*(["-user_agent", agent] if agent else []), *(["-headers", extra] if extra else []),
            "-protocol_whitelist", "http,https,tcp,tls,crypto,data", *disguised]


def _hls_output(prefix, first):
    # frag_discont writes the real time into every fragment. Without it a restarted encoder's fragments count from 0
    # and its start goes in the init segment, which the TV already has from the first run and won't fetch again.
    return ["-f", "hls", "-hls_time", str(SEGMENT), "-hls_list_size", "3", "-hls_flags",
            "temp_file+independent_segments", "-hls_segment_options", "movflags=+frag_discont",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename",
            f"{prefix}_init.mp4", "-hls_segment_filename", f"{prefix}%05d.m4s", "-start_number", str(first),
            f"{prefix}.m3u8"]


def subtitle_filter(begin):
    """Draws ../subs.ass onto the frames. The picture's clock restarts at 0 wherever the encoder starts, but the
    subtitles are timed against the whole video, so the clock is moved forward for the drawing and back after."""
    return f"setpts=PTS+{begin}/TB,subtitles=../subs.ass:fontsdir=../fonts,setpts=PTS-{begin}/TB"


def hdr_stage():
    """The GPU stage that lifts the highlights and tags the result as BT.2020 PQ in 10 bits."""
    return f"libplacebo=format=p010le:custom_shader_path=../hdr.glsl:{HDR_TAGS}"


def hdr_filter():
    """The HDR stage for a picture that has come back to the PC (to be drawn on, or never upscaled): it arrives as
    ordinary 8-bit SDR, so it is labelled as such, sent back to the GPU and fetched again."""
    return ("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,hwupload,"
            f"{hdr_stage()},hwdownload,format=p010le")


def dim_subtitles(ass_text):
    """Subtitles are drawn before the highlights are lifted, so plain white words would come out at the peak
    brightness. A light grey ends up near where they'd be on an ordinary screen."""
    return re.sub(r"^(Style:.*)$", lambda m: m.group(1).replace("&Hffffff", "&Hc8c8c8"), ass_text, flags=re.M)


def command(exe, url, headers, kind, first, audio, mode="fast", subs=False, hdr=False):
    """The FFmpeg command that makes pieces from number `first` on. It runs inside a folder next to shader.glsl (and
    subs.ass and fonts/, when there are subtitles), so no Windows path has to survive FFmpeg's filter syntax."""
    begin = first * SEGMENT
    # -bf 0: B-frames would shift a stream that starts at 0:00 by a few frames against its audio, and against the
    # pieces of a restarted encoder, which don't get that shift.
    if mode == BURN:
        picture = (subtitle_filter(begin) + ",") * bool(subs) + "format=yuv420p"
    else:
        # Upscale and HDR with nothing drawn between them stay on the GPU: copying every 4K frame back to the PC and up
        # again between the two cost a third of the speed (1.15x real time against 1.6x). 10 bits between them, so
        # lifting the highlights doesn't stretch 8-bit steps into bands.
        chained = hdr and not subs
        picture = (f"format=yuv420p,hwupload,libplacebo=w={TARGET_W}:h={TARGET_H}:force_original_aspect_ratio=decrease:"
                   f"force_divisible_by=2:format={'p010le' if chained else 'nv12'}:custom_shader_path=../shader.glsl")
        if chained:
            picture += f",{hdr_stage()},hwdownload,format=p010le"
        else:
            picture += ",hwdownload,format=nv12"
            if subs:
                picture += ",format=yuv420p," + subtitle_filter(begin)
    device = ["-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"] if (mode != BURN or hdr) else []
    if hdr and (mode == BURN or subs):                  # those pictures are on the PC, as yuv420p, and go back up
        picture += "," + hdr_filter()
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "warning", *device,
           *_input_options(headers, kind),
           "-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
           "-readrate", str(READ_RATE), "-readrate_initial_burst", str(BURST), "-ss", str(begin), "-i", url,
           "-an", "-sn", "-dn", "-vf", picture,
           "-c:v", "hevc_nvenc", "-preset", "p4", "-profile:v", "main10" if hdr else "main", "-level:v", "5.1",
           "-tier", "main", "-bf", "0",
           "-rc", "vbr", "-cq", "24", "-maxrate", str(MAX_BITRATE), "-bufsize", str(2 * MAX_BITRATE),
           "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT})", "-forced-idr", "1", "-tag:v", "hvc1",
           *(["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"] if hdr else []),
           "-output_ts_offset", str(begin), *_hls_output("v", first)]
    if audio:
        # first_pts: a source's audio can begin after its picture, and pieces are cut from the first audio packet, so
        # without silence to fill that gap every audio piece would sit that far from where the playlist says it is
        cmd += ["-vn", "-sn", "-dn", "-af", "aresample=async=1:first_pts=0",
                "-c:a", "aac", "-ac", "2", "-b:a", str(AUDIO_BITRATE),
                "-output_ts_offset", str(begin), *_hls_output("a", first)]
    return cmd


def master_playlist(audio, mode="fast", hdr=False):
    group = ',AUDIO="a"' if audio else ""
    size = "" if mode == BURN else f",RESOLUTION={TARGET_W}x{TARGET_H}"      # a subtitles-only cast keeps its own size
    codecs = ("hvc1.2.4.L153.B0" if hdr else "hvc1.1.6.L153.B0") + (",mp4a.40.2" if audio else "")
    size += ",VIDEO-RANGE=PQ" * bool(hdr)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    if audio:
        lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"')
    lines += [f"#EXT-X-STREAM-INF:BANDWIDTH={MAX_BITRATE + AUDIO_BITRATE},CODECS=\"{codecs}\"{size}{group}", "video.m3u8"]
    return "\n".join(lines) + "\n"


def media_playlist(prefix, duration):
    """The whole video as pieces, most of which don't exist yet."""
    total = max(1, math.ceil(duration / SEGMENT))
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{SEGMENT}", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS", f'#EXT-X-MAP:URI="{prefix}_init.mp4"']
    for n in range(total):
        length = min(SEGMENT, duration - n * SEGMENT) if duration > 0 else SEGMENT
        lines += [f"#EXTINF:{length:.3f},", f"{prefix}{n:05d}.m4s"]
    return "\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n"


def sweep():
    """Clear work folders a crashed run left behind."""
    root = tempfile.gettempdir()
    try:
        names = [n for n in os.listdir(root) if n.startswith(DIR_PREFIX)]
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if time.time() - os.path.getmtime(path) > OLD_DIRS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


class UpscaleSession:
    """One upscaled cast. It answers to the same fields as media_relay.Session, but its pieces come from the encoder
    instead of the site. Starts encoding as soon as it's made; close() stops it and deletes the pieces."""

    kind, entry_name = "hls", "index.m3u8"

    def __init__(self, url, headers, kind, token, start, duration, mode, audio, log=lambda m: None, subs=None,
                 hdr=False):
        self.url, self.headers, self.source_kind, self.token = url, headers, kind, token
        self.mode, self.subs, self.hdr = mode, bool(subs), bool(hdr)
        self.duration, self.audio, self.log = float(duration), bool(audio), log
        self.start, self.actual_start = float(start or 0.0), (None if start else 0.0)
        self.created, self.hits, self.error, self.failures = time.time(), 0, "", []
        self.stalls, self.closed = 0, False     # stalls: pieces in a row made slower than real time, while they're needed
        self._pace = None                       # (piece the encoder had reached, when)
        self._sent = [0, 0, 0.0]                # pieces, bytes, seconds
        self._waited = 0.0                      # seconds video requests have spent waiting for the encoder
        self.total = max(1, math.ceil(self.duration / SEGMENT))
        self.first = min(int(self.start // SEGMENT), self.total - 1)
        self.last = self.first                  # the latest piece the TV asked for
        self.eof = None                         # the piece after the source's real end, once an encoder reached it
        self.proc, self.run_dirs, self.run_first, self.reached = None, [], 0, 0
        self._tidied = None
        self._cond = threading.Condition()
        self.dir = tempfile.mkdtemp(prefix=DIR_PREFIX)
        if mode != BURN:
            shutil.copyfile(shader_path(mode), os.path.join(self.dir, "shader.glsl"))
        if hdr:
            with open(os.path.join(self.dir, "hdr.glsl"), "w", newline="\n") as f:
                f.write(hdr_shader())
        if subs:
            shutil.copyfile(subs, os.path.join(self.dir, "subs.ass"))
            if hdr:
                with open(subs, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
                with open(os.path.join(self.dir, "subs.ass"), "w", encoding="utf-8", newline="") as f:
                    f.write(dim_subtitles(text))
            fonts = os.path.join(self.dir, "fonts")
            os.mkdir(fonts)
            for name in os.listdir(font_source()):
                if name.lower().endswith(".ttf"):
                    shutil.copyfile(os.path.join(font_source(), name), os.path.join(fonts, name))
        with self._cond:
            self._start(self.first)
        threading.Thread(target=self._watch, name="upscale-watch", daemon=True).start()

    # ---- what the relay asks for ---------------------------------------------------
    def playlist(self, name):
        """Text of a playlist this session serves, or None if `name` isn't one."""
        if name == self.entry_name:
            return master_playlist(self.audio, self.mode, self.hdr)
        if name == "video.m3u8":
            return media_playlist("v", self.duration)
        if name == "audio.m3u8" and self.audio:
            return media_playlist("a", self.duration)
        return None

    def file_for(self, name, timeout=WAIT):
        """Path of a piece or init segment, waiting for the encoder if it's on its way. None if it can't be had."""
        m = PIECE.match(name)
        if not m and name not in ("v_init.mp4", "a_init.mp4"):
            return None
        piece = int(m.group(2)) if m else None
        began = time.time()
        with self._cond:
            if piece is not None:
                if piece >= self.total or (self.eof is not None and piece >= self.eof):
                    return None
                # Only the picture says where the TV is. Audio comes out of FFmpeg well ahead of it, so the TV's reads
                # run ahead too; that's not a seek unless it's asking for audio that's nowhere near being made.
                if m.group(1) == "v" or (not self._find(name) and not self._reachable(piece)):
                    self._steer(piece)
            while True:
                path = self._find(name)
                if path:
                    if m and m.group(1) == "v":
                        self._waited += time.time() - began
                    return path
                left = began + timeout - time.time()
                if self.closed or self.error or left <= 0:
                    return None
                self._cond.wait(min(left, 0.2))

    def wait_ready(self, check=lambda: None, timeout=45.0):
        """Block until the first piece exists, so a failure can fall back to a plain cast before the TV is involved.
        `check` may raise to abandon the wait. True if it's ready."""
        end, name = time.time() + timeout, f"v{self.first:05d}.m4s"
        with self._cond:
            while time.time() < end:
                check()
                if self._find(name):
                    return True
                if self.error or self.closed:
                    return False
                self._cond.wait(0.2)
        if not self.error:
            self.error = "the first piece took too long"
        return False

    def sent(self, size, seconds):
        """A video piece has gone to the TV: note how fast, since a TV short of network looks just like a slow PC."""
        with self._cond:
            total = self._sent
            total[0], total[1], total[2] = total[0] + 1, total[1] + size, total[2] + seconds
            if total[0] >= SENT_EVERY:
                need = total[1] * 8 / (total[0] * SEGMENT) / 1e6
                got = total[1] * 8 / max(total[2], 0.001) / 1e6
                self.log(f"upscale: stream needs {need:.1f} Mbit/s, TV took it at {got:.1f} Mbit/s, "
                         f"waited {self._waited:.1f}s for {total[0]} pieces, encoder {self._frontier() - self.last} ahead")
                self._sent, self._waited = [0, 0, 0.0], 0.0

    def close(self):
        with self._cond:
            self.closed = True
            self._stop_encoder()
            self._cond.notify_all()
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- the encoder ----------------------------------------------------------------
    def _start(self, first):
        self._stop_encoder()
        exe = shutil.which("ffmpeg")
        if not exe:
            self.error = "ffmpeg not found"
            return
        run = os.path.join(self.dir, f"run{len(self.run_dirs) + 1}")
        os.mkdir(run)
        self.run_dirs.append(run)
        self.run_first, self.reached, self._pace = first, first, None
        self.log(f"upscale: encoding from {first * SEGMENT}s")
        with open(os.path.join(run, "ffmpeg.log"), "wb") as err:
            self.proc = subprocess.Popen(command(exe, self.url, self.headers, self.source_kind, first, self.audio,
                                                   self.mode, self.subs, self.hdr),
                                         cwd=run, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                         creationflags=NO_WINDOW | QUIET_PRIORITY)

    def _stop_encoder(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _find(self, name):
        for run in reversed(self.run_dirs):             # the newest copy, if a restart made a piece twice
            path = os.path.join(run, name)
            if os.path.isfile(path):
                return path
        return None

    def _head(self, piece):
        """The first piece from `piece` on that doesn't exist yet."""
        while piece < self.total and self._find(f"v{piece:05d}.m4s"):
            piece += 1
        return piece

    def _frontier(self):
        """The first piece this encoder hasn't made yet. It's counted forward from where it last was and never from
        the start of the run, because the pieces the TV has finished with get deleted."""
        self.reached = self._head(max(self.reached, self.run_first))
        return self.reached

    def _reachable(self, piece):
        """Whether the running encoder will get to `piece` soon enough to wait for it."""
        return self.run_first <= piece <= self._frontier() + JUMP

    def _steer(self, piece):
        """The TV wants `piece`: keep the encoder on course for it."""
        if self.closed or self.error:
            return
        self.last = piece
        head = self._head(piece)
        if head > piece:                                # already made; just top the buffer up if the encoder stopped
            if self.proc is None and head < self.total and head - piece < REFILL and self.eof is None:
                self._start(head)
            return
        if self.proc is None or not self._reachable(piece):          # a jump, or nothing is running
            self._start(piece)
        self._cond.notify_all()

    def _watch(self):
        while not self.closed:
            time.sleep(0.25)
            with self._cond:
                if self.closed:
                    return
                self._check_encoder()
                self._tidy()
                self._cond.notify_all()

    def _check_encoder(self):
        proc = self.proc
        if proc is None:
            return
        rc = proc.poll()
        reached = self._frontier()
        head = self._head(max(self.last, self.run_first))
        if rc is None:
            self._time_pieces(head)
            if head - self.last > AHEAD:
                self.log(f"upscale: {AHEAD * SEGMENT}s ahead of the TV, resting")
                self._stop_encoder()
            return
        self.proc = None
        if reached == self.run_first:
            self.error = self._last_line() or f"FFmpeg exited with code {rc}"
            self.log(f"upscale failed: {self.error}")
        elif rc == 0 and reached >= self.total - 2:
            self.eof = reached                          # the real end; a playlist's length is only ever approximate
        else:                                           # FFmpeg can exit 0 on a dropped source: the next ask restarts
            self.log(f"upscale: encoder stopped early (code {rc}): {self._last_line()}")

    def _time_pieces(self, head):
        """Keep count of pieces taking longer to make than they take to watch, while the TV is close behind them.
        Not of how long the TV waits: it reads ahead as fast as it can, so it's nearly always waiting for something."""
        now = time.time()
        if self._pace is None or head < self._pace[0]:
            if head > self.run_first:                   # the first piece's time is mostly FFmpeg starting up
                self._pace = (head, now)
            return
        reached, when = self._pace
        if head > reached:
            slow = (now - when) / (head - reached) > SEGMENT and head - self.last < REFILL
            self.stalls = self.stalls + 1 if slow else 0
            self._pace = (head, now)

    def _last_line(self):
        try:
            with open(os.path.join(self.run_dirs[-1], "ffmpeg.log"), "rb") as f:
                lines = [l for l in f.read().decode(errors="replace").splitlines() if l.strip()]
            return lines[-1][:200] if lines else ""
        except OSError:
            return ""

    def _tidy(self):
        """Keep the disk use to the few minutes around the TV."""
        if self.last == self._tidied:
            return
        self._tidied = self.last
        self._frontier()                                # before anything it would have to count through is deleted
        for run in self.run_dirs:
            try:
                names = os.listdir(run)
            except OSError:
                continue
            for name in names:
                m = PIECE.match(name)
                if m and not self.last - KEEP_BEHIND <= int(m.group(2)) <= self.last + 2 * AHEAD:
                    try:
                        os.remove(os.path.join(run, name))
                    except OSError:
                        pass                            # being sent to the TV right now; next time
