"""CapoCast - GUI. Roku private-listening audio on this PC, plus a YouTube tab (embedded Chrome, auto-cast).

Reuses the protocol helpers from roku_listen.py. Instead of ffplay, audio is decoded by ffmpeg to raw PCM,
scaled by the volume slider in Python (audioop) and played with sounddevice, so volume works live.
"""
import asyncio
import base64
import collections
import ctypes
import io
import audioop
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from tkinter import messagebox, ttk

import sounddevice as sd
from PIL import Image, ImageOps, ImageTk
import websockets

import discord_presence
import health
import phone_remote
import phone_store
import roku_find
import tv_apps
import web_tab
import yt_embed
from types import SimpleNamespace
from yt_bridge import Bridge

from roku_listen import (
    DEFAULT_HOST, ECP_PORT, FFPLAY_PORT, RTCP_PORT, RTCP_RR, RTP_PORT, SDP,
    auth_response, check_support, local_ip_for, rtcp_app,
)

CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen.json")
CHUNK = 960 * 4  # 20 ms of 48 kHz stereo s16le
RR_EVERY = 1.0   # seconds between RTCP receiver reports; the Roku drops the stream if they're too sparse
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen.log")
PHONE_STORE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RokuListen.phone.json")   # bookmarks, history
PLAYLET_ID = tv_apps.PLAYLET_ID   # the Roku channel id of Playlet, which plays the YouTube tab's videos


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}" + chr(10))
    except OSError:
        pass


SILENCE_AFTER = 3.0     # seconds without audio packets before the status says so
SOUND_DELAY_MAX = 1500  # the Sound delay slider's top end, ms
BUFFER_MS = 300         # sound held back before playing: the TV's Wi-Fi pauses for up to about 250 ms every 10 s
MAX_FILL = 100          # a longer gap (a second of 10 ms packets) is an outage, not something to paper over
DEVICE_RETRY = 30.0     # seconds to keep trying to reopen a lost audio output before giving up


def output_devices():
    """Names of the sound outputs, de-duplicated (Windows lists each device once per audio API)."""
    try:
        seen = []
        for d in sd.query_devices():
            if d["max_output_channels"] > 0 and d["name"] not in seen:
                seen.append(d["name"])
        return seen
    except Exception:
        return []


def find_output_device(name):
    """sounddevice index of the output called `name`, or None (system default) if unset or not present."""
    if not name:
        return None
    try:
        default = sd.default.hostapi
        match = [i for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0 and d["name"] == name]
        return next((i for i in match if sd.query_devices(i)["hostapi"] == default), match[0] if match else None)
    except Exception:
        return None


def filler_packets(prev, packet):
    """RTP packets standing in for the ones lost between `prev` and `packet`, so the decoder produces a frame for each
    and the sound keeps its length instead of shrinking by what the network dropped.

    Each carries only the Opus TOC byte of the packet before the gap (code 0, an empty frame), which libopus treats as a
    lost frame: it conceals it rather than leaving a hole. The RTP timestamps are spread evenly between the two real
    packets; they don't have to divide exactly (the Roku's often don't), since the decoder counts frames, not times.
    [] if nothing is missing, if `packet` is out of order, or if the gap is too long to be worth filling."""
    missing = ((int.from_bytes(packet[2:4], "big") - int.from_bytes(prev[2:4], "big")) & 0xFFFF) - 1
    if missing < 1 or missing > MAX_FILL or len(prev) <= 12:
        return []
    t0 = int.from_bytes(prev[4:8], "big")
    span = (int.from_bytes(packet[4:8], "big") - t0) & 0xFFFFFFFF
    step = span // (missing + 1) if 0 < span < 0x80000000 else 480        # 480 = 10 ms at 48 kHz
    seq = int.from_bytes(prev[2:4], "big")
    toc = bytes([prev[12] & 0xFC])
    return [prev[:2] + ((seq + i) & 0xFFFF).to_bytes(2, "big") + ((t0 + i * (step or 480)) & 0xFFFFFFFF).to_bytes(4, "big")
            + prev[8:12] + toc for i in range(1, missing + 1)]


_LIBOPUS = {}


def has_libopus(ffmpeg):
    """Whether this FFmpeg can decode Opus with libopus (checked once)."""
    if ffmpeg not in _LIBOPUS:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW).stdout
            _LIBOPUS[ffmpeg] = any(line.split()[1:2] == ["libopus"] for line in out.splitlines() if line.strip())
        except Exception:
            _LIBOPUS[ffmpeg] = False
    return _LIBOPUS[ffmpeg]


def decoder_command(ffmpeg, sdp_path, codec="libopus"):
    """FFmpeg reading the Roku's RTP from `sdp_path` and writing raw PCM to stdout. `codec` picks the Opus decoder:
    only libopus conceals lost frames, FFmpeg's own decoder doesn't."""
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning", "-protocol_whitelist", "file,udp,rtp",
            "-fflags", "nobuffer", "-flags", "low_delay"] + (["-c:a", codec] if codec else []) +            ["-i", sdp_path, "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]


class AudioBuffer:
    """Decoded audio waiting for the sound card, so a pause in the stream (the TV's Wi-Fi hiccups for about a quarter of
    a second every few seconds) is covered by what's already queued instead of being heard as a gap.

    A reader thread moves FFmpeg's output in. Playing starts once `target_ms` is queued, and after running dry it waits
    to refill before starting again (counted as an underrun). The TV's clock and the sound card's never match exactly,
    so anything more than `spill_ms` above the target is dropped, oldest first."""

    def __init__(self, read, target_ms, stop_evt, spill_ms=150):
        self.read, self.stop_evt = read, stop_evt
        self.target, self.spill = max(0, target_ms) / 1000, spill_ms / 1000
        self.q, self.cond = collections.deque(), threading.Condition()
        self.eof = self.primed = False
        self.underruns = self.dropped = 0
        threading.Thread(target=self._fill, name="audio-buffer", daemon=True).start()

    def level(self):
        """Seconds of audio queued."""
        return len(self.q) * CHUNK / (48000 * 4)

    def finished(self):
        return self.eof and not self.q

    def retarget(self, seconds):
        """Change how much is held back, while playing. More: it goes quiet for the difference and resumes later (the
        sound is now later). Less: what is queued beyond the new amount is dropped (the sound is now earlier)."""
        with self.cond:
            self.target = max(0.0, seconds)
            if self.level() < self.target and self.q:
                self.primed = False                       # wait for the extra, without counting an underrun
            while self.level() > self.target + self.spill:
                self.q.popleft()
                self.dropped += 1
            self.cond.notify_all()

    def _fill(self):
        while not self.stop_evt.is_set():
            try:
                buf = self.read(CHUNK)
            except Exception:                  # the pipe closed under us (ffmpeg gone, session ending)
                buf = b""
            with self.cond:
                if not buf:
                    self.eof = True
                    self.cond.notify_all()
                    return
                self.q.append(buf)
                while self.level() > self.target + self.spill:
                    self.q.popleft()
                    self.dropped += 1
                self.cond.notify_all()

    def get(self, timeout=0.1):
        """The next chunk to play, or None if there isn't one yet (still filling, or the stream has ended: see finished())."""
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                if self.q and (self.primed or self.level() >= self.target or self.eof):
                    self.primed = True
                    return self.q.popleft()
                if self.primed and not self.q and not self.eof:
                    self.primed = False
                    self.underruns += 1
                left = end - time.monotonic()
                if left <= 0 or self.finished():
                    return None
                self.cond.wait(left)


class RtpStats:
    """Counts what actually arrives from the Roku, by RTP sequence number, so a dropout can be told apart as lost on
    the network (gaps here too) or lost after it reached this app (gaps only in FFmpeg's log)."""

    def __init__(self):
        self.last = None
        self.reset()

    def reset(self):
        self.packets = self.lost = self.gaps = self.reordered = self.concealed = 0
        self.longest_pause, self._at = 0.0, None

    def packet(self, seq, now):
        self.packets += 1
        if self._at is not None:
            self.longest_pause = max(self.longest_pause, now - self._at)
        self._at = now
        if self.last is not None:
            step = (seq - self.last) & 0xFFFF
            if step == 0 or step > 0x8000:
                self.reordered += 1
                return
            if step > 1:
                self.lost += step - 1
                self.gaps += 1
        self.last = seq

    def report(self):
        text = (f"rtp: {self.packets} packets, {self.lost} missing in {self.gaps} gaps, {self.reordered} late/duplicate, "
                f"longest pause {self.longest_pause * 1000:.0f} ms, {self.concealed} concealed")
        self.reset()
        return text


class Remote:
    """Transport controls over Roku's ECP (port 8060); Playlet's API (8888) gives exact seeking."""

    @staticmethod
    def _open(url, data=None, headers=None, timeout=3, method=None):
        method = method or ("POST" if data is not None else "GET")
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=timeout).read()

    @classmethod
    def key(cls, host, name):
        cls._open(f"http://{host}:{ECP_PORT}/keypress/{name}", data=b"")

    @classmethod
    def state(cls, host):
        """-> {state, app, pos, dur} in seconds, or None if nothing is playing / unreachable."""
        try:
            root = ET.fromstring(cls._open(f"http://{host}:{ECP_PORT}/query/media-player"))
        except Exception:
            return None
        st = root.get("state", "none")
        if st in ("none", "close", "stop"):
            return {"state": "none", "app": "", "pos": 0, "dur": 0}
        ms = lambda tag: (int((root.findtext(tag) or "0").split()[0]) / 1000)
        plugin = root.find("plugin")
        return {"state": st, "app": plugin.get("name", "") if plugin is not None else "",
                "pos": ms("position"), "dur": ms("duration")}

    @classmethod
    def playlet_now(cls, host):
        """Playlet's now-playing entry ({videoid, title, ...}), or {}."""
        try:
            return json.loads(cls._open(f"http://{host}:8888/api/queue")).get("nowplaying") or {}
        except Exception:
            return {}

    @classmethod
    def _playlet_jump(cls, host, st, target):
        """Restart Playlet's current video at `target` seconds. False if it isn't playing something seekable."""
        vid = cls.playlet_now(host).get("videoid") if st and st["app"] == "Playlet" and st["dur"] > 0 else None
        if not vid:
            return False
        body = json.dumps({"videoId": vid, "timestamp": int(max(0, min(target, st["dur"] - 2)))}).encode()
        cls._open(f"http://{host}:8888/api/queue/play", data=body,
                  headers={"Content-Type": "application/json", "X-Playlet": "1"})
        return True

    @classmethod
    def seek(cls, host, delta):
        st = cls.state(host)
        if st and cls._playlet_jump(host, st, st["pos"] + delta):
            return
        cls.key(host, "Right" if delta > 0 else "Left")  # most other apps skip ~10s on the arrow keys

    @classmethod
    def seek_to(cls, host, target):
        """Jump to an exact time (YouTube in Playlet only; other apps have no such command)."""
        if not cls._playlet_jump(host, cls.state(host), target):
            raise ValueError("This app can't jump to a time. Use the skip buttons.")

    @classmethod
    def clear_queue(cls, host):
        """Empty Playlet's queue (best effort). It closes the player too, so call it only right before playing something.

        Playlet keeps every video it has played, and a video it plays is inserted *before* whatever was queued after
        it. After a few hours the queue held 2600 entries: reading it took the Roku 1.6 s per request (this app reads
        it every few seconds), and when a video ended Playlet moved on to an older song by itself."""
        try:
            cls._open(f"http://{host}:8888/api/queue", headers={"X-Playlet": "1"}, method="DELETE", timeout=3)
        except OSError as e:
            log(f"couldn't clear Playlet's queue: {e!r}")

    @classmethod
    def cast(cls, host, video_id, timestamp=0, fresh=False):
        """Play one YouTube video in Playlet (same call the Chrome extension makes), starting `timestamp` seconds in.

        Never pass a playlist: given a playlistId Playlet ignores the videoId and the timestamp and plays the playlist
        from its first entry, after downloading all of it - the TV went back to the first song and took seconds to start.
        The YouTube tab moves on to the next song itself, and that is cast like any other video.
        fresh=True: this is a different video than the one playing (not a seek inside it), so start from an empty queue."""
        body = {"videoId": video_id}
        if timestamp > 3:
            body["timestamp"] = int(timestamp)
        if fresh:
            cls.clear_queue(host)
        # Playlet answers slowly while it loads a video; a 3 s limit made a busy PC/TV report failure for a cast that
        # went through, and the retry then reloaded the video a second time.
        send = lambda: cls._open(f"http://{host}:8888/api/queue/play", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Playlet": "1"}, timeout=8)
        try:
            send()
        except urllib.error.HTTPError:
            raise
        except TimeoutError:
            raise            # Playlet took the request and is slow to answer: sending it again would load the video twice
        except OSError:
            # Playlet's server only runs while Playlet is on screen. After a Web-tab cast (or any other app) the
            # TV is somewhere else, so open Playlet and try once more.
            cls._ensure_playlet(host)
            send()

    @classmethod
    def _ensure_playlet(cls, host):
        cls._open(f"http://{host}:{ECP_PORT}/launch/{PLAYLET_ID}", data=b"")
        for _ in range(24):
            time.sleep(0.5)
            try:
                cls._open(f"http://{host}:8888/api/state", timeout=2)
                return
            except OSError:
                pass

    @classmethod
    def active_app(cls, host):
        """ECP id of the app on screen ('dev' is the sideloaded RokuCast channel), or ''."""
        try:
            app = ET.fromstring(cls._open(f"http://{host}:{ECP_PORT}/query/active-app")).find("app")
            return app.get("id", "") if app is not None else ""
        except Exception:
            return ""

    @classmethod
    def cast_url(cls, host, url):
        """Play an http(s) URL in the RokuCast channel: launch it, or if it's already running hand it the URL."""
        enc = urllib.parse.quote(url, safe="")
        running = cls.active_app(host) == "dev"
        path = f"/input?url={enc}" if running else f"/launch/dev?url={enc}"
        try:
            cls._open(f"http://{host}:{ECP_PORT}{path}", data=b"", timeout=6)
        except urllib.error.HTTPError as e:
            if e.code == 404 and not running:
                raise RuntimeError("The RokuCast channel isn't installed on this Roku (the Web tab plays video "
                                   "through it).")
            if e.code == 403:
                raise RuntimeError("The Roku refused the command. On the Roku: Settings > System > Advanced system "
                                   "settings > Control by mobile apps > Network access > Permissive.")
            raise RuntimeError(f"The Roku answered HTTP {e.code}.")
        except OSError as e:
            raise RuntimeError(f"Couldn't reach the Roku at {host}: {e}")

    @classmethod
    def apps(cls, host):
        """[{'id', 'name'}] of the apps installed on the Roku."""
        return tv_apps.parse_apps(cls._open(f"http://{host}:{ECP_PORT}/query/apps", timeout=4))

    @classmethod
    def icon(cls, host, app_id):
        """The app's picture (PNG bytes) as the Roku's own home screen draws it."""
        return cls._open(f"http://{host}:{ECP_PORT}/query/icon/{urllib.parse.quote(app_id, safe='')}", timeout=4)

    @classmethod
    def launch(cls, host, app_id):
        cls._open(f"http://{host}:{ECP_PORT}/launch/{urllib.parse.quote(app_id, safe='')}", data=b"", timeout=6)

    @classmethod
    def close_player(cls, host):
        """Clear Playlet's queue, which also closes its player and returns to Playlet's home screen."""
        cls._open(f"http://{host}:8888/api/queue", headers={"X-Playlet": "1"}, method="DELETE")


class KeySender:
    """Sends keypresses to the Roku one at a time, in order.

    Typing needs a single queue: a thread per key (like the transport buttons use) lets the HTTP requests
    overtake each other, so "hello" could arrive as "hlelo".
    """

    def __init__(self, host_fn):
        self.host_fn, self.error = host_fn, ""
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def key(self, name):
        self.q.put(name)

    def text(self, s):
        for ch in s:
            if ch.isprintable():
                self.q.put("Lit_" + urllib.parse.quote(ch, safe=""))   # ECP takes one URL-encoded char per press

    def _run(self):
        while True:
            name = self.q.get()
            try:
                Remote.key(self.host_fn(), name)
                self.error = ""
            except Exception as e:
                log(f"keypress {name} failed: {e!r}")
                if isinstance(e, urllib.error.HTTPError) and e.code == 403:
                    self.error = "Roku blocked typing: allow Control by mobile apps"
                else:
                    self.error = "Can't reach the Roku"
                with self.q.mutex:          # don't sit through a 3 s timeout for every queued key
                    self.q.queue.clear()


class KeyboardWatch:
    """Hears when a text field opens on the TV, the same signal the official phone app uses to pop up its keyboard.

    Keeps its own ecp-2 session open (the listening one only exists while audio is connected), subscribes to the
    textedit events and calls on_change({"text", "masked"}) while a field is open, or on_change(None).
    """

    EVENTS = "+textedit-opened,+textedit-changed,+textedit-closed,+volume-changed"

    def __init__(self, host_fn, on_change, on_volume=None):
        self.host_fn, self.on_change, self.on_volume = host_fn, on_change, on_volume
        self.stop_evt = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_evt.set()

    def _run(self):
        delay = 2
        while not self.stop_evt.is_set():
            try:
                asyncio.run(self._session(self.host_fn()))
                delay = 2                       # left on purpose (IP changed): reconnect promptly
            except Exception as e:
                log(f"keyboard watch: {e!r}")
                delay = min(30, delay * 2)      # TV asleep / unreachable: back off
            self.on_change(None)
            self.stop_evt.wait(delay)

    def _emit(self, d, prefix):
        tid = d.get(prefix + "textedit-id")
        if tid in (None, "none"):
            self.on_change(None)
        else:
            self.on_change({"text": d.get(prefix + "text", ""), "masked": d.get(prefix + "masked") == "true"})

    async def _session(self, host):
        async with websockets.connect(f"ws://{host}:{ECP_PORT}/ecp-session", subprotocols=["ecp-2"],
                                      additional_headers={"Sec-WebSocket-Origin": "Android"},
                                      open_timeout=5) as ws:
            while not self.stop_evt.is_set() and self.host_fn() == host:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
                except asyncio.TimeoutError:
                    continue
                note, resp = msg.get("notify"), msg.get("response")
                if note == "authenticate":
                    await ws.send(json.dumps({"request": "authenticate", "request-id": "0",
                                              "param-response": auth_response(msg["param-challenge"])}))
                elif resp == "authenticate":
                    if msg.get("status") != "200":
                        raise RuntimeError(f"auth refused: {msg}")
                    await ws.send(json.dumps({"request": "request-events", "request-id": "1",
                                              "param-events": self.EVENTS}))
                    # a field may already be open when we connect
                    await ws.send(json.dumps({"request": "query-textedit-state", "request-id": "2"}))
                elif resp == "request-events" and msg.get("status") not in ("200", "202"):
                    raise RuntimeError(f"TV won't send keyboard events: {msg}")
                elif resp == "query-textedit-state" and msg.get("status") == "200":
                    state = json.loads(base64.b64decode(msg["content-data"])).get("textedit-state", {})
                    self._emit(state, "")
                elif note in ("textedit-opened", "textedit-changed"):
                    self._emit(msg, "param-")
                elif note == "textedit-closed":
                    self.on_change(None)
                elif note == "volume-changed" and self.on_volume:
                    try:
                        self.on_volume({"volume": int(msg.get("param-volume", "")), "mute": msg.get("param-mute") == "true",
                                        "dest": msg.get("param-audio-destination", "")})
                    except ValueError:
                        pass


MIN_W, MIN_H = 1200, 700      # the top bar (tabs, nav, Auto-cast, Discord, Resync, status) needs this much width


def restore_geometry(saved, screen, min_size=(MIN_W, MIN_H)):
    """(w, h, x, y, zoomed) to open the window with, from what was saved last time, or None to centre it as usual.

    `screen` is the whole desktop across monitors, (x, y, w, h). A window saved on a monitor that has since been
    unplugged would open out of reach, so it is only restored if its title bar is on a screen now."""
    try:
        w, h, x, y = (int(saved[k]) for k in ("w", "h", "x", "y"))
        zoomed = bool(saved.get("zoomed"))
        sx, sy, sw, sh = screen
    except (TypeError, KeyError, ValueError):
        return None
    w, h = max(min_size[0], min(w, sw)), max(min_size[1], min(h, sh))
    if x + w < sx + 120 or x > sx + sw - 120 or y < sy - 20 or y > sy + sh - 80:
        return None
    return w, h, x, y, zoomed


def virtual_screen():
    """The whole desktop across monitors as (x, y, w, h), or None if Windows won't say."""
    try:
        gm = ctypes.windll.user32.GetSystemMetrics
        w, h = gm(78), gm(79)
        return (gm(76), gm(77), w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def resync_plan(tv, tv_vid, pc, current):
    """What the Resync button should do, from where the TV and the PC's player each are. Pure, so every case is tested.

    tv: Remote.state() or None; tv_vid: the video id Playlet is playing ('' if unknown); pc: Bridge.player() or None;
    current: the video id CastSync last put on the TV. Moving the PC player is instant and reloading the TV costs a
    rebuffer, so the TV is the reference unless it lost the video. Returns (action, why):
      nothing | ad | buffering | align | follow (PC opens the TV's video) | recast (TV gets the PC's video)."""
    if not pc:
        return "nothing", "Nothing is playing in the YouTube tab. Start a video there first."
    if pc.get("ad"):
        return "ad", "An ad is playing in the YouTube tab. Try again when it ends."
    tv_on = bool(tv) and tv["app"] == "Playlet" and tv["state"] in ("play", "pause", "buffer")
    if not tv_on:
        return "recast", "The TV had stopped, so the PC's video was sent to it again"
    vid = tv_vid or current
    if vid and vid != pc["id"]:
        if pc["id"] == current:
            return "follow", "The TV had moved on, so the PC followed it"
        return "recast", "The TV was on a different video, so the PC's video was sent to it"
    if tv["state"] == "buffer":
        return "buffering", "The TV is still buffering"
    return "align", ""


class CastSync:
    """Mirrors the app's YouTube player onto Playlet and keeps the two lined up.

    Events come from the extension and run in order on one thread. Playlet can only seek by reloading the video
    (a 2-3 s rebuffer), so a seek is sent to where the PC player *will be* once the TV is playing again (`lead`,
    learned from how long Playlet actually takes). Afterwards the muted PC player is nudged onto the TV's timeline:
    nudging the PC is instant, re-seeking the TV never is.
    """

    health = None   # health.Health, set by the app: counts reloads for the connection health panel

    def _count(self, skipped, detail):
        if self.health:
            self.health.reload(skipped, detail)

    SETTLE = 3.0    # after a (re)load Playlet still reports the previous stream's state for a moment
    DRIFT = 0.6     # seconds of PC-vs-TV difference worth correcting
    RECHECK = 20.0  # seconds between drift checks while both are playing
    APPLY = 0.3     # a nudge reaches the page on its next poll (every 250 ms)

    def __init__(self, host_fn, bridge, on_now=None):
        self.host_fn, self.bridge = host_fn, bridge
        self.on_now = on_now         # called with (title, video id) when a video starts on the TV, (None, None) when it ends
        self.q = queue.Queue()
        self.title, self.error = "", ""
        self.settle_until = 0.0
        self.lead = 2.5          # learned: seconds from "play at t" until the TV is really playing
        self.current = None      # video id on the TV
        self.last_check = 0.0
        self._last_seek = None   # (id, t, when) of the last seek sent to the TV
        self._nudge = None       # (id, target, when) of the last time the PC player was moved onto the TV's timeline
        self._retry = 0          # quick re-checks left when the TV couldn't be measured (it was buffering or paused)
        self.note, self.note_until, self.note_ok = "", 0.0, True    # the result of the last Resync, for the status text
        self.syncing = False
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, event):
        self.q.put(event)

    def _next(self):
        ev = self.q.get(timeout=1.0)
        while ev.get("kind") in ("seek", "load", "leave"):
            with self.q.mutex:
                queued = self.q.queue
                if ev["kind"] == "seek":          # scrubbing: only the latest of several queued seeks matters
                    stale = bool(queued) and queued[0].get("kind") == "seek"
                else:                             # clicking through songs: a load (or leave) a newer load will replace
                    stale = any(e.get("kind") == "load" for e in queued)   # costs the TV a rebuffer for nothing
                if not stale:
                    break
                if ev["kind"] != "seek":
                    log(f"sync {ev['kind']} {ev.get('id')} skipped: a newer video was already asked for")
                ev = queued.popleft()
        return ev

    def _run(self):
        while True:
            try:
                ev = self._next()
            except queue.Empty:
                if self.current and time.time() - self.last_check > self.RECHECK:
                    self._safe("align", lambda: self._align(self.host_fn()))
                continue
            self._safe(ev.get("kind"), lambda: self._handle(ev))

    def _safe(self, what, fn):
        try:
            fn()
            self.error = ""
        except Exception as e:
            log(f"sync error on {what}: {e!r}")
            self.error = "Can't reach Playlet on the Roku"

    def _handle(self, ev):
        host, kind = self.host_fn(), ev.get("kind")
        log(f"sync {kind} {ev.get('id')} t={ev.get('t')}")
        if kind in ("seek", "pause", "play") and ev.get("id") != self.current:
            # An event about a video the TV isn't on (the page was already on the next song, or the load for it failed).
            # Acting on it would seek or toggle whatever the TV is playing now.
            log(f"sync {kind} skipped: {ev.get('id')} is not the video on the TV")
            return
        if kind == "load":
            if self._already_playing(host, ev["id"], ev.get("t", 0), ev.get("resume", False)):
                log("sync load skipped: the TV is already playing this video at about this point")
                self._count(True, f"load {ev.get('id')} at {ev.get('t', 0):.0f}s")
                self.current, self.title = ev["id"], ev.get("title") or ev["id"]
                self._announce(self.title, self.current)
                self._align(host)
                return
            t0 = time.time()
            self._count(False, f"load {ev.get('id')} at {ev.get('t', 0):.0f}s")
            Remote.cast(host, ev["id"], ev.get("t", 0), fresh=True)
            self.title, self.current = ev.get("title") or ev["id"], ev["id"]
            self._announce(self.title, self.current)
            self.settle_until = t0 + self.SETTLE
            if self._started(host, ev.get("t", 0), t0):
                self._align(host)
        elif kind == "resync":
            try:
                ok, text = self._resync(host)
                self._say(text, ok)
            except Exception:
                self._say("Couldn't reach the TV to resync", False)
                raise
            finally:
                self.syncing = False
        elif kind == "seek":
            t, last = ev.get("t", 0), self._last_seek
            if last and last[0] == ev["id"] and abs(t - last[1]) < 1.0 and time.time() - last[2] < 6:
                log("sync seek skipped: YouTube's own correction of the previous seek")
                return
            nudge = self._nudge
            if nudge and nudge[0] == ev["id"] and abs(t - nudge[1]) < 2.0 and time.time() - nudge[2] < self.ECHO:
                log("sync seek skipped: the page reporting the move this app just made")
                return
            self._last_seek = (ev["id"], t, time.time())
            paused = ev.get("paused")
            target = max(0.0, ev.get("t", 0) + (0 if paused else self.lead))
            t0 = time.time()
            self._count(False, f"seek {ev.get('id')} to {target:.0f}s")
            Remote.cast(host, ev["id"], target)   # Playlet seeks by replaying at a timestamp
            self.settle_until = t0 + self.SETTLE
            if paused:
                self._ensure(host, "pause")
            elif self._started(host, target, t0):
                self._align(host)
        elif kind in ("pause", "play"):
            self._ensure(host, kind)
            if kind == "play":
                self._align(host)
        elif kind == "leave":
            Remote.close_player(host)   # Playlet closes its player and shows its home screen
            self.title, self.current = "", None
            self._announce(None, None)

    def adopt(self, video_id, title=""):
        """The TV is already playing `video_id` (it was started from the phone, not by this player). Learn that, so when
        the PC opens the same video the repeated "load" is recognised and doesn't reload it on the TV."""
        if video_id and video_id != self.current:
            self.current, self.title = video_id, title or video_id

    def _announce(self, title, video_id):
        if self.on_now:
            try:
                self.on_now(title, video_id)
            except Exception as e:
                log(f"on_now failed: {e!r}")

    ALREADY = 12.0  # a "load" this close to where the TV already is, for the same video, is not worth a reload

    def _already_playing(self, host, video_id, t, resume=False):
        """True if the TV is already playing `video_id` near `t`. Reloading it in Playlet is a 2-3 s rebuffer, so a
        repeated "load" (the page re-announcing a video that is already on the TV) must not cost one.

        Playlet is asked what it plays, not CastSync's memory of it: the TV can be ahead (it moved on to the next song
        itself, or the phone started it). resume: the page only just started following (it was reloaded, or Auto-cast
        came on mid-video), so wherever its clock is, the TV is the one to trust and it is not worth a jump."""
        st = Remote.state(host)
        if not st or st["app"] != "Playlet" or st["state"] not in ("play", "pause", "buffer"):
            return False
        if not resume and abs(st["pos"] - t) > self.ALREADY:
            return False
        return Remote.playlet_now(host).get("videoid") == video_id

    def _started(self, host, target, t0):
        """Wait (abandoning it if a newer event arrives) until the TV is really playing near target; learn the lead."""
        last = None
        while time.time() < t0 + 10 and self.q.empty():
            st = Remote.state(host)
            if st and st["state"] == "play" and abs(st["pos"] - target) < 6:
                if last is not None and st["pos"] > last:      # the position is moving: it's actually playing
                    took = time.time() - t0 - 0.1                # ~half a poll interval of detection delay
                    self.lead = min(6.0, max(0.5, 0.6 * self.lead + 0.4 * took))
                    log(f"tv playing after {took:.2f}s, lead now {self.lead:.2f}s")
                    return True
                last = st["pos"]
            time.sleep(0.2)
        return False

    def _tv_position(self, host):
        """The TV's position right now. Roku updates it in steps, so wait for a step and extrapolate from it."""
        first = Remote.state(host)
        if not first or first["state"] != "play":
            return None
        start = time.time()
        while time.time() - start < 1.5:
            time.sleep(0.05)
            st = Remote.state(host)
            if not st or st["state"] != "play":
                return None
            if st["pos"] != first["pos"]:
                return st["pos"]          # fresh reading, taken just now
        return first["pos"] + (time.time() - start)

    def _align(self, host):
        """Snap the muted PC player onto the TV's timeline if they've drifted apart."""
        self.last_check = time.time()
        p = self.bridge.player()
        if not p or p["id"] != self.current or p["paused"]:
            return
        tv = self._tv_position(host)
        p = self.bridge.player()          # re-read: _tv_position took a moment
        if tv is None and self._retry < 5:
            # The TV wasn't playing (buffering, still starting): look again in 2 s, not after the usual 20 s
            self._retry += 1
            self.last_check = time.time() - self.RECHECK + 2.0
        elif tv is not None:
            self._retry = 0
        if tv is None or not p or p["id"] != self.current or p["paused"]:
            return
        if Remote.playlet_now(host).get("videoid") != self.current:
            # The TV is on a different video (it picked the next one, or someone used the remote). Its clock says nothing
            # about this one: following it jumped the PC to a random point, and the page's seek then reloaded the TV.
            log("align skipped: the TV is not playing the video the PC is on")
            return
        drift = tv - p["t"]
        if abs(drift) > self.DRIFT:
            self._move_pc(tv + self.APPLY)
            log(f"aligned PC player to TV (was {drift:+.2f}s off)")

    ECHO = 8.0       # seconds during which a seek the page reports near our own nudge is that nudge coming back

    def _move_pc(self, target, paused=None):
        """Put the muted PC player at `target` (and play or pause it) without the page echoing a seek to the TV."""
        cmd = {"sync": round(target, 2), "syncId": self.current}
        if paused is not None:
            cmd["syncPaused"] = bool(paused)
        self._nudge = (self.current, target, time.time())
        self.bridge.send(cmd)

    # ---- the Resync button -----------------------------------------------------
    def request_resync(self):
        """Ask for a resync (from the window or the phone). False if one is already running."""
        if self.syncing:
            return False
        self.syncing = True
        self.q.put({"kind": "resync"})
        return True

    def _say(self, text, ok=True, seconds=6):
        self.note, self.note_until, self.note_ok = text, time.time() + seconds, ok
        log(f"resync: {text}")

    RESYNC_TOLERANCE = 0.4     # seconds: close enough that nobody could tell (a nudge lands within about 0.3)
    RESYNC_ROUNDS = 3

    def _resync(self, host):
        """Line the PC's player and the TV up now, whichever way they drifted. Returns what to tell the user."""
        pc = self.bridge.player()
        st = Remote.state(host)
        vid = Remote.playlet_now(host).get("videoid") or "" if st and st["app"] == "Playlet" else ""
        action, why = resync_plan(st, vid, pc, self.current)
        if action in ("nothing", "ad"):
            return False, why
        if action == "recast":                       # the TV lost the video: send it the PC's, where the PC is now
            target = pc["t"] + (0 if pc["paused"] else self.lead)
            t0 = time.time()
            self._count(False, f"resync recast {pc['id']} at {target:.0f}s")
            Remote.cast(host, pc["id"], target, fresh=True)
            self.current, self.title = pc["id"], self.title if pc["id"] == self.current else pc["id"]
            self._announce(self.title, self.current)
            self.settle_until = t0 + self.SETTLE
            if pc["paused"]:
                self._ensure(host, "pause")
            else:
                self._started(host, target, t0)
            self._nudge = None
            return True, why
        if action == "follow":                       # the TV moved on (autoplay, phone, remote): the PC opens its video
            now = Remote.playlet_now(host)
            pos = (st["pos"] if st else 0) + 4.0     # the page takes a few seconds to load and start
            if st and st["dur"]:
                pos = min(pos, max(0.0, st["dur"] - 5))
            self.adopt(vid, now.get("title") or "")
            self._announce(self.title, self.current)
            self.bridge.send({"navigate": f"https://www.youtube.com/watch?v={urllib.parse.quote(vid, safe='')}"
                                          f"&t={int(pos)}s"})
            self._nudge = None
            return True, why
        if action == "buffering":                    # give the TV a moment to start before measuring it
            end = time.time() + 6
            while time.time() < end:
                time.sleep(0.5)
                st = Remote.state(host)
                if st and st["state"] != "buffer":
                    break
            if not st or st["state"] == "buffer":
                return False, why
        return self._resync_align(host)

    def _resync_align(self, host):
        """Move the PC player onto the TV's timeline, then measure again and repeat: a YouTube seek lands a little late,
        so one shot is rarely exact."""
        first = None
        for _ in range(self.RESYNC_ROUNDS):
            st = Remote.state(host)
            pc = self.bridge.player()
            if not st or not pc or pc["id"] != self.current:
                return False, "Lost track of one of the two players; try again"
            if st["state"] == "pause":                 # nothing moves on the TV: its reading is exact
                tv, tv_paused = st["pos"], True
            else:
                tv, tv_paused = self._tv_position(host), False
                if tv is None:
                    return False, "The TV isn't playing yet; try again in a moment"
            pc = self.bridge.player()                  # re-read: measuring the TV took a moment
            if not pc:
                return False, "Lost track of the YouTube tab; try again"
            drift = tv - pc["t"]
            first = drift if first is None else first
            if abs(drift) <= self.RESYNC_TOLERANCE and pc["paused"] == tv_paused:
                break
            self._move_pc(tv + (0 if tv_paused else self.APPLY), paused=tv_paused)
            time.sleep(1.4)                            # the page applies it on its next poll, then settles
        else:
            pc, st = self.bridge.player(), Remote.state(host)
            left = abs((st["pos"] if st else 0) - pc["t"]) if pc and st else 0
            if left > 1.0:
                return False, f"Moved the PC {abs(first or 0):.1f} s, but it is still about {left:.1f} s apart"
        self._retry = 0
        self.last_check = time.time()
        if abs(first or 0) <= self.RESYNC_TOLERANCE:
            return True, "Already in step"
        return True, f"In step again (the PC was {abs(first):.1f} s {'behind' if first > 0 else 'ahead'})"

    def _ensure(self, host, want):
        """The Roku Play key toggles, so only press it when the TV is in the other state."""
        time.sleep(max(0.0, self.settle_until - time.time()))
        deadline = time.time() + 8
        while time.time() < deadline:
            st = Remote.state(host)
            state = st and st["state"]
            if state in ("play", "pause"):
                if state != want:
                    Remote.key(host, "Play")
                return
            time.sleep(0.4)   # buffering / starting up


class TvVolume:
    """The TV's own volume, for when the sound is playing on the Roku and not routed to this PC.

    The Roku can't be asked what its volume is, but it announces every change (volume, mute and where the sound is
    going), so the level is tracked from those announcements, wherever the change came from: this app or the remote.
    It can't be set directly either, only nudged with VolumeUp / VolumeDown, so set_target() presses those until the
    announced level matches. The first press also tells us where we started."""

    def __init__(self, host_fn, on_change):
        self.host_fn, self.on_change = host_fn, on_change
        self.level, self.muted, self.dest = None, False, ""
        self.target = None
        self._moved, self._wake = threading.Event(), threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def heard(self, d):
        self.level, self.muted, self.dest = d["volume"], d["mute"], d["dest"]
        self._moved.set()
        self.on_change()

    def set_target(self, v):
        self.target = max(0, min(100, int(v)))
        self._wake.set()

    def toggle_mute(self):
        def press():
            try:
                Remote.key(self.host_fn(), "VolumeMute")
            except Exception as e:
                log(f"tv mute failed: {e!r}")
        threading.Thread(target=press, daemon=True).start()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            stuck = 0
            while self.target is not None:
                target, level = self.target, self.level
                if level is not None and level == target:
                    if self.target == target:
                        self.target = None
                    break
                self._moved.clear()
                try:
                    Remote.key(self.host_fn(), "VolumeUp" if (level is None or target > level) else "VolumeDown")
                except Exception as e:
                    log(f"tv volume press failed: {e!r}")
                    self.target = None
                    break
                if self._moved.wait(0.8):
                    stuck = 0
                else:                                  # no announcement: at the limit, or the TV isn't listening
                    stuck += 1
                    if stuck >= 2:
                        self.target = None
                        break


class Listener:
    """One private-listening session: websocket + RTP socket + ffmpeg decoder + audio output."""

    def __init__(self, host, delay_ms, on_status, buffer_ms=0):
        # `delay` is what the Roku is asked to buffer on its side (VDLY). The sound buffer here is separate and adds to it:
        # the sound reaches the speakers about delay + buffer behind the TV's own clock.
        self.host, self.delay_us, self.on_status = host, delay_ms * 1000, on_status
        self.buffer_ms, self.buffer, self.concealing = buffer_ms, None, False
        self.out_latency = 0.0                             # seconds the sound card itself holds
        self.stats, self.last_report = None, ""
        self.volume = 1.0
        self.device, self.reopen = "", False   # output device name ('' = system default); reopen: switch it live
        self.stop_evt = threading.Event()
        self.threads, self.sock, self.proc, self.sdp_path, self.loop = [], None, None, None, None
        self.ws_task, self.failed = None, False

    def _fail(self, text):
        self.failed = True
        self.on_status(text, ended=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH")
        try:
            check_support(self.host)
        except SystemExit as e:
            raise RuntimeError(str(e.code))
        except Exception as e:
            raise RuntimeError(f"Can't reach Roku at {self.host}: {e}")
        self.my_ip = local_ip_for(self.host)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", RTP_PORT))

        f = tempfile.NamedTemporaryFile("w", suffix=".sdp", delete=False)
        f.write(SDP)
        f.close()
        self.sdp_path = f.name
        self.concealing = has_libopus(ffmpeg)
        log("decoder: " + ("libopus, lost packets are concealed" if self.concealing
                           else "FFmpeg's own Opus decoder (no libopus): lost packets leave gaps"))
        self.proc = subprocess.Popen(
            decoder_command(ffmpeg, self.sdp_path, "libopus" if self.concealing else ""),
            # its own file: a child's inherited handle doesn't really append on Windows, so it overwrites log() lines
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=open(LOG + ".ffmpeg", "w"), creationflags=NO_WINDOW)

        for fn in (self._rtp_loop, self._audio_loop, self._ws_thread):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)
        self.on_status("Connecting…")

    def stop(self):
        self.stop_evt.set()
        if self.loop and self.ws_task:
            self.loop.call_soon_threadsafe(self.ws_task.cancel)
        for obj in (self.sock,):
            try:
                obj and obj.close()
            except OSError:
                pass
        if self.proc:
            self.proc.terminate()
        if self.sdp_path:
            try:
                os.unlink(self.sdp_path)
            except OSError:
                pass

    # ---- worker threads --------------------------------------------------
    def _rtp_loop(self):
        fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        roku = (self.host, RTCP_PORT)
        self.sock.settimeout(0.5)
        started, last_rr, last_pkt, count = False, 0.0, time.monotonic(), 0
        silent = False
        stats = self.stats = RtpStats()
        last_report = time.monotonic()
        prev = None                            # the last packet that moved forward, to fill any gap after it

        def send(data, addr, sock=self.sock):
            # Windows reports an earlier send to a closed port as an error on the next receive, and a send can fail
            # while the network blips. Neither is a reason to stop listening.
            try:
                sock.sendto(data, addr)
            except OSError as e:
                if not self.stop_evt.is_set():
                    log(f"rtp send failed: {e!r}")

        while not self.stop_evt.is_set():
            now = time.monotonic()
            # Keep the Roku's keepalive satisfied on a timer (not only when packets arrive).
            if started and now - last_rr >= RR_EVERY:
                send(RTCP_RR, roku)
                last_rr = now
            if started and now - last_report >= 60:
                last_report = now
                buf = self.buffer
                self.last_report = stats.report() + (f", buffer {buf.level() * 1000:.0f} ms, {buf.underruns} underruns, "
                                                     f"{buf.dropped} dropped" if buf else "")
                log(self.last_report)
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                if started and now - last_pkt > 1.5:
                    log(f"no RTP for {now - last_pkt:.1f}s (packets so far: {count})")
                if started and not silent and now - last_pkt > SILENCE_AFTER:
                    silent = True
                    self.on_status("No audio from the Roku")
                continue
            except ConnectionResetError:
                continue                       # see send(): not a real failure
            except OSError:
                if self.stop_evt.is_set():
                    break
                time.sleep(0.05)               # the socket itself is gone only when stop() closed it
                continue
            if len(data) < 12 or data[0] >> 6 != 2:
                continue
            last_pkt, count = time.monotonic(), count + 1
            stats.packet(int.from_bytes(data[2:4], "big"), last_pkt)
            try:
                if self.concealing and prev is not None:
                    fill = filler_packets(prev, data)
                    for f in fill:                 # stand-ins for what the network lost, so the sound stays on time
                        fwd.sendto(f, ("127.0.0.1", FFPLAY_PORT))
                    stats.concealed += len(fill)
                fwd.sendto(data, ("127.0.0.1", FFPLAY_PORT))
            except OSError:
                pass
            if prev is None or ((int.from_bytes(data[2:4], "big") - int.from_bytes(prev[2:4], "big")) & 0xFFFF) < 0x8000:
                prev = data
            if silent:
                silent = False
                self.on_status("Connected - playing")
            if not started:
                started = True
                send(rtcp_app(b"VDLY", self.delay_us), roku)
                send(rtcp_app(b"CVER", 808464434), roku)
                send(RTCP_RR, roku)
                last_rr = time.monotonic()
                log("first RTP packet received")
                self.on_status("Connected - playing")

    def _audio_loop(self):
        """Decoded audio to the speakers. If the output device goes away (unplugged, driver reset, a sleep/wake) it
        is reopened for up to DEVICE_RETRY seconds instead of ending the session."""
        gave_up_at = None
        if self.buffer is None:
            self.buffer = AudioBuffer(lambda n: self.proc.stdout.read(n), self.buffer_ms, self.stop_evt)
        while not self.stop_evt.is_set():
            try:
                with sd.RawOutputStream(samplerate=48000, channels=2, dtype="int16", blocksize=960, latency="low",
                                        device=self.device_index()) as out:
                    gave_up_at = None
                    self.reopen = False
                    self.out_latency = float(getattr(out, "latency", 0) or 0)
                    self.buffer.retarget(self.buffer_ms / 1000)
                    while not self.stop_evt.is_set() and not self.reopen:
                        buf = self.buffer.get()
                        if buf is None:
                            if not self.buffer.finished():
                                continue                 # still filling: look at stop / reopen again
                            if not self.stop_evt.is_set():
                                log(f"ffmpeg exited rc={self.proc.poll()}")
                                self._fail("Audio decoder stopped - see RokuListen.log in %APPDATA%")
                            return
                        v = self.volume
                        out.write(buf if v == 1.0 else audioop.mul(buf, 2, v))
                if self.reopen:
                    log("audio: switching output device")
                    continue
            except Exception as e:
                if self.stop_evt.is_set():
                    return
                gave_up_at = gave_up_at or time.monotonic() + DEVICE_RETRY
                if time.monotonic() > gave_up_at:
                    log(f"audio error: {e!r}")
                    self._fail(f"Audio error: {e}")
                    return
                log(f"audio device problem, retrying: {e!r}")
                self.on_status("Audio device lost, reconnecting…")
                self._rescan_devices()
                self.stop_evt.wait(1.5)
                if not self.stop_evt.is_set():
                    self.on_status("Connected - playing")

    def set_buffer(self, ms):
        """Move the sound earlier or later right now (from the Sound delay slider)."""
        self.buffer_ms = max(0, int(ms))
        if self.buffer is not None:
            self.buffer.retarget(self.buffer_ms / 1000)

    def device_index(self):
        """The saved output device's index, or None for the system default (also when it isn't plugged in now)."""
        return find_output_device(self.device)

    @staticmethod
    def _rescan_devices():
        """PortAudio only lists the devices present when it started; restart it so a reconnected one is seen."""
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            log(f"audio rescan failed: {e!r}")

    def _ws_thread(self):
        self.loop = asyncio.new_event_loop()
        try:
            self.ws_task = self.loop.create_task(self._session())
            self.loop.run_until_complete(self.ws_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self.stop_evt.is_set():
                self.on_status(f"Roku connection lost: {e}")
        finally:
            if not self.stop_evt.is_set() and not self.failed:
                self.on_status("Disconnected by Roku", ended=True)

    async def _session(self):
        uri = f"ws://{self.host}:{ECP_PORT}/ecp-session"
        async with websockets.connect(uri, subprotocols=["ecp-2"],
                                      additional_headers={"Sec-WebSocket-Origin": "Android"}) as ws:
            async for text in ws:
                msg = json.loads(text)
                if msg.get("notify") == "authenticate":
                    await ws.send(json.dumps({"request": "authenticate", "request-id": "0",
                                              "param-response": auth_response(msg["param-challenge"])}))
                elif msg.get("response") == "authenticate":
                    if msg.get("status") != "200":
                        self._fail("Roku rejected authentication")
                        return
                    await ws.send(json.dumps({"request": "set-audio-output", "request-id": "1",
                                              "param-audio-output": "datagram",
                                              "param-devname": f"{self.my_ip}:{RTP_PORT}"}))
                elif msg.get("response") == "set-audio-output" and msg.get("status") != "200":
                    self._fail(f"Roku refused the stream ({msg.get('status-msg')})")
                    return


from ui_kit import (   # noqa: E402  (the look lives in ui_kit.py; re-exported here for the tests and the tabs)
    ACCENT, ACCENT_HI, AMBER, AppTile, BG, CARD, Chip, DPad, Dot, EDGE, FG, FONT, FONT_MED, GREEN, GREY, IconButton, MUTED,
    NavButton, PRESSED, ProgressBar, RAISED, RED, RoundButton, Slider, TabSwitch, Toggle, Tooltip, asset, dot_picture,
    dark_titlebar, fit_text, oval, rounded, text_y,
)
BAR_H = 60


# Tk keysym -> Roku ECP key for the non-text keys the Type box forwards; the value's glyph is shown as feedback
ECP_KEYS = {
    "Up": ("Up", "↑"), "Down": ("Down", "↓"), "Left": ("Left", "←"), "Right": ("Right", "→"),
    "Return": ("Select", "OK"), "KP_Enter": ("Select", "OK"), "Escape": ("Back", "Back"),
    "BackSpace": ("Backspace", "⌫"), "Delete": ("Backspace", "⌫"),
}
CTRL, ALT = 0x4, 0x20000   # Tk event.state bits on Windows (AltGr arrives as Ctrl+Alt)


class TypeBox(tk.Frame):
    """Click it, then type: text goes into whatever text field is open on the TV, and arrows / Enter / Esc
    work the TV's menus like the remote's d-pad, OK and Back."""

    def __init__(self, parent, sender, width=328, height=58):
        super().__init__(parent, bg=CARD, width=width, height=height, highlightthickness=1, takefocus=1,
                         highlightbackground=EDGE, highlightcolor=ACCENT, cursor="xterm")
        self.pack_propagate(False)
        self.sender, self.typed, self.last = sender, "", ""
        self.tv = None   # {"text", "masked"} while a text field is open on the TV (from KeyboardWatch)
        self.line = tk.Label(self, bg=CARD, font=(FONT, 11), anchor="w")
        self.hint = tk.Label(self, bg=CARD, font=(FONT, 8), anchor="w")
        self.line.pack(fill="x", padx=12, pady=(8, 0))
        self.hint.pack(fill="x", padx=12)
        for w in (self, self.line, self.hint):
            w.bind("<Button-1>", lambda e: self.focus_set())
        self.bind("<KeyPress>", self._key)
        self.bind("<FocusIn>", lambda e: self.render())
        self.bind("<FocusOut>", lambda e: self.render())
        self.render()

    def owns(self, widget):
        return widget in (self, self.line, self.hint)

    @staticmethod
    def wants(e):
        """True for keys the box would send (so a stray press elsewhere can be handed to it)."""
        if e.state & CTRL and not e.state & ALT:
            return e.keysym.lower() == "v"
        return e.keysym in ECP_KEYS or bool(e.char and e.char.isprintable())

    def set_tv(self, state):
        if state is None:
            self.typed, self.last = "", ""
        self.tv = state
        self.render()

    def _key(self, e):
        if e.keysym in ("Tab", "ISO_Left_Tab"):
            return None                                   # keep normal focus traversal
        if e.state & CTRL and not e.state & ALT:
            if e.keysym.lower() == "v":
                self._paste()
            return "break"
        if e.keysym in ECP_KEYS:
            name, self.last = ECP_KEYS[e.keysym]
            self.sender.key(name)
            if name == "Backspace":
                self.typed = self.typed[:-1]
            elif name in ("Select", "Back"):
                self.typed = ""                           # that text is done; start fresh next time
        elif e.char and e.char.isprintable():
            self.sender.text(e.char)
            self.typed, self.last = self.typed + e.char, ""
        else:
            return None                                   # Shift, F-keys, etc.
        self.render()
        return "break"

    def _paste(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        text = " ".join(text.split())                     # newlines / tabs can't be typed into a TV field
        self.sender.text(text)
        self.typed, self.last = self.typed + text, "paste"
        self.render()

    def render(self):
        try:
            focused = self.focus_get() is self
        except KeyError:   # focus is inside the embedded Chrome, which Tk doesn't know
            focused = False
        if self.tv is not None:   # the TV's own text is the truth; passwords stay hidden
            text = self.tv["text"]
            text = "•" * len(text) if self.tv["masked"] else text
        else:
            text = self.typed
        if text or focused:
            shown = text if len(text) <= 30 else "…" + text[-29:]
            self.line.config(text=shown + ("▏" if focused else ""), fg=FG)
        else:
            self.line.config(text="Click here to type on the TV", fg=MUTED)
        last = f"  ·  sent {self.last}" if self.last else ""
        if self.sender.error:
            self.hint.config(text=self.sender.error, fg=RED)
        elif self.tv is not None:
            self.hint.config(text=("TV keyboard open · Enter = OK · Esc = Back" + last) if focused
                             else "TV keyboard open · click here to type", fg=GREEN)
        elif focused:
            self.hint.config(text=f"Arrows move · Enter = OK · Esc = Back{last}", fg=MUTED)
        else:
            self.hint.config(text="Search and sign-in boxes, Ctrl+V pastes", fg=MUTED)
        self.config(highlightbackground=GREEN if self.tv is not None and not focused else EDGE)


class AppRow(tk.Frame):
    """Pinned apps in two columns under the remote, plus an "All apps" tile for the rest of what the Roku has.

    Everything slow (the app list, the pictures, launching) runs on a thread; Tk objects are only made on the Tk thread."""

    GAP, VGAP, RETRY = 8, 6, 30.0     # gaps between tiles across and down; seconds between retries

    def __init__(self, parent, pins, on_pins, host_fn, log=lambda m: None):
        super().__init__(parent, bg=BG)
        self.pins, self.on_pins, self.host_fn, self.log = list(pins), on_pins, host_fn, log
        self.apps, self._raw, self._pics = [], {}, {}
        self._host, self._tried, self._busy = None, 0.0, False
        self.grid_box = tk.Frame(self, bg=BG)
        self.grid_box.pack(anchor="w")
        self.hint = tk.Label(self, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.hint.pack(fill="x", pady=(6, 0))
        self._hint_job = None
        self.render()

    # ---- drawing -----------------------------------------------------------------
    def render(self):
        for w in self.grid_box.winfo_children():
            w.destroy()
        tiles = []
        for app_id in self.pins:
            name = tv_apps.name_for(self.apps, app_id)
            tiles.append(AppTile(self.grid_box, name, lambda a=app_id: self.launch(a), self._pics.get(app_id),
                                 on_menu=lambda e, a=app_id: self._menu(e, a)))
        tiles.append(AppTile(self.grid_box, "All apps", self.open_all, accent=True))
        for i, tile in enumerate(tiles):
            tile.grid(row=i // 2, column=i % 2, padx=(0 if i % 2 == 0 else self.GAP, 0),
                      pady=(0 if i < 2 else self.VGAP, 0))

    def say(self, text, color=MUTED, seconds=4):
        self.hint.config(text=text, fg=color)
        if self._hint_job:
            self.after_cancel(self._hint_job)
        self._hint_job = self.after(int(seconds * 1000), lambda: self.hint.config(text=""))

    def _menu(self, e, app_id):
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#ffffff", bd=0)
        menu.add_command(label="Unpin", command=lambda: self.toggle(app_id))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # ---- loading -------------------------------------------------------------------
    def tick(self, host):
        """Called every second: load the list when the Roku's address changes, and keep trying while it's unreachable."""
        if self._busy:
            return
        if host != self._host or (not self.apps and time.time() - self._tried > self.RETRY):
            self._host, self._tried, self._busy = host, time.time(), True
            threading.Thread(target=self._fetch, args=(host,), daemon=True).start()

    def _after(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):         # window already closed
            pass

    def _fetch(self, host):
        apps, raw = [], {}
        try:
            apps = Remote.apps(host)
            for app_id in self.pins:
                raw[app_id] = self._icon(host, app_id)
        except Exception as e:
            self.log(f"app list: {e!r}")
        self._after(lambda: self._loaded(host, apps, raw))

    @staticmethod
    def _icon(host, app_id):
        try:
            return Remote.icon(host, app_id)
        except Exception:
            return None

    def _loaded(self, host, apps, raw):
        self._busy = False
        if host != self._host:
            return
        if apps:
            self.apps = apps
        for app_id, data in raw.items():
            if data:
                self._raw[app_id] = data
        self._make_pictures()
        self.render()

    def _make_pictures(self):
        for app_id, data in self._raw.items():
            if app_id in self._pics:
                continue
            try:
                im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGBA"), (AppTile.W - 4, AppTile.H - 4),
                                  Image.LANCZOS)
                self._pics[app_id] = ImageTk.PhotoImage(im)
            except Exception as e:
                self.log(f"app picture {app_id}: {e!r}")

    # ---- actions -------------------------------------------------------------------
    def launch(self, app_id):
        host, name = self.host_fn(), tv_apps.name_for(self.apps, app_id)
        self.say(f"Opening {name}…")

        def work():
            try:
                Remote.launch(host, app_id)
            except Exception as e:
                self.log(f"launch {app_id} failed: {e!r}")
                self._after(lambda: self.say(f"Couldn't open {name}: is the Roku reachable?", RED, 6))
        threading.Thread(target=work, daemon=True).start()

    def toggle(self, app_id):
        self.pins, what = tv_apps.toggle_pin(self.pins, app_id)
        if what == "full":
            return self.say(f"{tv_apps.MAX_PINS} apps is the most: unpin one first", AMBER, 5)
        self.on_pins(self.pins)
        if what == "pinned" and app_id not in self._raw:
            host = self.host_fn()

            def work():
                data = self._icon(host, app_id)
                self._after(lambda: self._loaded(host, [], {app_id: data}))
            threading.Thread(target=work, daemon=True).start()
        self.render()

    def open_all(self):
        """A list of everything on the Roku, to open an app or pin it to the tiles."""
        win = tk.Toplevel(self)
        win.title("Roku apps")
        dark_titlebar(win)
        win.configure(bg=BG)
        win.transient(self.winfo_toplevel())
        win.geometry("340x440")
        want = tk.StringVar()
        entry = tk.Entry(win, textvariable=want, bg=CARD, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 11),
                         highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        entry.pack(fill="x", padx=16, pady=(16, 8), ipady=6, ipadx=6)
        lb = tk.Listbox(win, bg=CARD, fg=FG, selectbackground=ACCENT, selectforeground="#ffffff", relief="flat",
                        font=(FONT, 11), activestyle="none", highlightthickness=1, highlightbackground=EDGE)
        lb.pack(fill="both", expand=True, padx=16)
        info = tk.Label(win, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        info.pack(fill="x", padx=16, pady=(6, 0))
        shown = []

        def fill(*_):
            shown[:] = tv_apps.filter_apps(self.apps, want.get())
            lb.delete(0, "end")
            for a in shown:
                lb.insert("end", ("★  " if a["id"] in self.pins else "     ") + a["name"])
            if lb.size():
                lb.selection_set(0)
            info.config(text="" if self.apps else "Couldn't read the Roku's apps. Is it on and reachable?")

        def picked():
            sel = lb.curselection()
            return shown[sel[0]] if sel else None

        def open_it(_e=None):
            a = picked()
            if a:
                self.launch(a["id"])
                win.destroy()

        def pin_it():
            a, keep = picked(), lb.curselection()
            if a:
                self.toggle(a["id"])
                fill()
                if keep and keep[0] < lb.size():
                    lb.selection_clear(0, "end")
                    lb.selection_set(keep[0])

        row = tk.Frame(win, bg=BG)
        row.pack(pady=12)
        RoundButton(row, "Open", open_it, width=140, height=38).pack(side="left", padx=6)
        pin = RoundButton(row, "Pin / unpin", pin_it, width=140, height=38)
        pin.style("Pin / unpin", "#26262b", "#34343a")
        pin.pack(side="left", padx=6)
        want.trace_add("write", fill)
        lb.bind("<Double-Button-1>", open_it)
        entry.bind("<Return>", open_it)
        entry.bind("<Down>", lambda e: (lb.focus_set(), "break")[1])
        win.bind("<Escape>", lambda e: win.destroy())
        fill()
        entry.focus_set()


class PhoneBackend:
    """What the phone page can do, in terms of this app. Runs on the phone server's threads, so anything that touches
    the window is handed to the Tk thread."""

    def __init__(self, app):
        self.app, self._apps, self._apps_at, self._icons = app, [], 0.0, {}

    def state(self):
        app, web = self.app, self.app.web
        vol = {"where": "pc", "level": app.pc_volume, "muted": False} if app.listener else               {"where": "tv", "level": app.tvvol.level, "muted": app.tvvol.muted}
        note = web.phone_note()
        sn = app.sync
        if sn.note and time.time() < sn.note_until:             # what the Resync button just did
            note = (sn.note, sn.note_ok)
        return {"tv": app.last_tv, "now": self.now(app.last_tv), "volume": vol, "watching": web.phone_watching(),
                "note": {"text": note[0], "ok": note[1]} if note else None}

    def now(self, tv):
        """What the TV is playing, for the phone's card: title, position and length in the video's own time."""
        if not tv or tv["state"] == "none":
            return None
        web = self.app.web
        watching, sync = web.phone_watching(), web.sync
        if watching and sync:
            off = sync.offset
            dur = sync.dur or (tv["dur"] + off if tv["dur"] else 0)
            title = watching["title"] or web.guard.snapshot()["title"] or "Web video"
            return {"source": "web", "app": "Web", "title": title, "state": tv["state"], "pos": tv["pos"] + off,
                    "dur": dur, "seekable": bool(dur) and not sync.live, "pc": True}
        if tv["app"] == "Playlet":
            return {"source": "youtube", "app": "YouTube", "title": self._playlet_title(), "state": tv["state"],
                    "pos": tv["pos"], "dur": tv["dur"], "seekable": tv["dur"] > 0, "pc": True}
        return {"source": "tv", "app": tv["app"] or "Roku", "title": tv["app"] or "Roku", "state": tv["state"],
                "pos": tv["pos"], "dur": tv["dur"], "seekable": False, "pc": False}   # pc: can it also be watched on the PC

    def _playlet_title(self):
        """The YouTube video's title: Playlet's own entry, else looked up by id. Cached so polling stays cheap."""
        now = time.time()
        cache = getattr(self, "_yt_cache", None)
        if cache and now - cache[0] < 4:
            return cache[1]
        np = Remote.playlet_now(self.app._host)
        title = np.get("title") or ""
        vid = np.get("videoid") or ""
        if not title and vid:
            known = getattr(self, "_yt_titles", {})
            if vid not in known:
                try:
                    known[vid] = discord_presence.youtube_info(vid)[1] or ""
                except Exception:
                    known[vid] = ""
                self._yt_titles = known
            title = known[vid]
        self._yt_cache = (now, title or "YouTube")
        return self._yt_cache[1]

    PC_LEAD = 4.0        # seconds the page takes to load and start: the PC opens that far past where the TV is

    def watch_on_pc(self):
        """Also show what the TV is playing on the PC, so it can be watched on both screens. A Web cast is already in the
        Web tab's browser, so that tab is brought up; a YouTube video is opened in the YouTube tab at the TV's position.
        Returns the message for the phone."""
        app = self.app
        now = self.now(app.last_tv)
        if not now:
            raise ValueError("Nothing is playing on the TV.")
        if now["source"] == "web":
            app.after(0, app.show_web_on_pc)
            return "On the PC's Web tab now, following the TV"
        if now["source"] == "youtube":
            vid = Remote.playlet_now(app._host).get("videoid") or ""
            if not vid:
                raise ValueError("Couldn't tell which YouTube video is playing.")
            if app.chrome.alive() and not app.bridge.connected():
                raise ValueError("The PC's YouTube tab isn't linked to CapoCast (reload the extension in Chrome).")
            pos = now["pos"] + self.PC_LEAD
            if now["dur"]:
                pos = min(pos, max(0.0, now["dur"] - 5))
            app.after(0, lambda: app.watch_youtube_on_pc(vid, now["title"], pos))
            return "Opening it on the PC" + (", in step with the TV" if app.autocast else " (Auto-cast is off, so it won't stay in step)")
        raise ValueError(f"{now['app']} can't be opened on the PC. Only YouTube and Web casts can.")

    def resync(self):
        """The phone's Resync button: line the PC's YouTube player up with the TV again. Returns the message for the phone;
        what it found and did shows up in the phone's note a moment later."""
        app = self.app
        now = self.now(app.last_tv)
        if not now or now["source"] != "youtube":
            raise ValueError("Resync is for a YouTube video playing on the TV.")
        if not app.autocast:
            raise ValueError("Turn Auto-cast on in CapoCast first: it is what links the PC's player to the TV.")
        if not app.bridge.connected():
            raise ValueError("The PC's YouTube tab isn't linked to CapoCast (reload the extension in Chrome).")
        if not app.sync.request_resync():
            return "Already resyncing"
        return "Resyncing the PC with the TV…"

    def seek_to(self, t):
        app, web = self.app, self.app.web
        tv = app.last_tv
        if web.phone_watching() and tv and web.sync:
            return web.phone_transport("skip", t - (tv["pos"] + web.sync.offset))   # the page leads; the TV follows it
        Remote.seek_to(app._host, t)

    def key(self, name):
        if name == "Play" and self.app.web.phone_watching():
            return self.app.web.phone_transport("toggle")   # while casting, the page's video leads and the TV follows it
        self.app.keys.key(name)                       # the same queue as typing, so order is kept

    def text(self, s):
        self.app.keys.text(s)

    def seek(self, delta):
        if self.app.web.phone_watching():
            return self.app.web.phone_transport("skip", delta)
        Remote.seek(self.app._host, delta)

    def volume(self, step):
        app = self.app
        if app.listener:
            app.after(0, lambda: app._phone_pc_volume(step * 5))
        else:
            for _ in range(abs(step)):
                app.keys.key("VolumeUp" if step > 0 else "VolumeDown")

    def launch(self, app_id):
        Remote.launch(self.app._host, app_id)

    def apps(self):
        if time.time() - self._apps_at > 30 or not self._apps:
            self._apps, self._apps_at = Remote.apps(self.app._host), time.time()
        return self._apps

    def playlet(self):
        """Where the phone's YouTube tab finds Playlet's web app, and whether the Roku is serving it right now (it only
        does while Playlet is open on the TV). The phone loads it straight from the Roku, so its address is the Roku's."""
        host = self.app._host
        up, account = False, ""
        try:
            data = json.loads(Remote._open(f"http://{host}:8888/api/profiles", timeout=1.5))
            up = True
            now = next((p for p in data.get("profiles", []) if p.get("id") == data.get("currentProfile")), {})
            account = now.get("username") or ""        # whose feed Playlet is showing (it is the TV's profile too)
        except Exception:
            pass
        return {"up": up, "url": f"http://{host}:8888/", "id": PLAYLET_ID, "account": account}

    def icon(self, app_id):
        if app_id not in self._icons:
            self._icons[app_id] = Remote.icon(self.app._host, app_id)
        return self._icons[app_id]

    def cast(self, url):
        yt = phone_remote.youtube_target(url)
        if yt:
            vid, _playlist, start = yt        # the link's video, not its playlist (Playlet would play the playlist's first)
            Remote.cast(self.app._host, vid, start, fresh=True)
            return "Playing on the TV"
        return self.app.web.cast_link(url)

    # ---- the Browse tab: the Web tab's page, driven from the phone ----
    def web_frame(self, after, w, h):
        return self.app.web.phone_frame(after, w, h)

    def web_info(self):
        return self.app.web.phone_info()

    def web_tap(self, x, y):
        self.app.web.phone_tap(x, y)

    def web_scroll(self, x, y, dx, dy):
        self.app.web.phone_scroll(x, y, dx, dy)

    def web_text(self, text):
        self.app.web.phone_text(text)

    def web_key(self, name):
        self.app.web.phone_key(name)

    def web_nav(self, action, url):
        self.app.web.phone_nav(action, url)

    def web_cast(self):
        return self.app.web.phone_cast()

    def web_youtube(self, on, go):
        self.app.web.phone_youtube(on, go)

    def web_transport(self, action, value):
        self.app.web.phone_transport(action, value)

    def web_pages(self):
        return self.app.web.phone_pages()

    def web_bookmark(self, action, url):
        return self.app.web.phone_bookmark(action, url)

    def web_continue(self, action, url):
        return self.app.web.phone_continue(action, url)

    def stop_cast(self):
        web = self.app.web
        self.app.after(0, lambda: web.cancel_cast() if web.busy else web.stop_casting())


class HealthWindow(tk.Toplevel):
    """The connection health panel: one row each for the TV, Playlet, the YouTube link, the audio and recent stalls,
    refreshed every second, with a button that copies the lot as text."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Connection health")
        self.configure(bg=BG)
        self.geometry("560x560")
        self.minsize(460, 420)
        tk.Label(self, text="Connection health", bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        tk.Label(self, text="The last 30 minutes. A stall names who froze: this app, the browser, or the link between them.",
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=520, justify="left").pack(anchor="w", padx=18, pady=(0, 10))
        card = tk.Frame(self, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x", padx=18)
        self.rows = {}
        for key, label in (("tv", "TV"), ("playlet", "Playlet"), ("sync", "YouTube sync"), ("buffer", "TV buffering"),
                           ("stalls", "Stalls"), ("audio", "Audio")):
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=14, pady=5)
            dot = Dot(row, 10, CARD)
            dot.pack(side="left", pady=2)
            item = None
            tk.Label(row, text=label, bg=CARD, fg=MUTED, font=(FONT, 10), width=13, anchor="w").pack(side="left", padx=(8, 0))
            text = tk.Label(row, bg=CARD, fg=FG, font=(FONT, 10), anchor="w", justify="left", wraplength=340)
            text.pack(side="left", fill="x", expand=True)
            self.rows[key] = (dot, item, text)
        self.copy_btn = RoundButton(self, "Copy report", self.copy, width=140, height=36)
        self.copy_btn.pack(side="bottom", anchor="e", padx=18, pady=12)       # packed first, so a long list can't push it out
        tk.Label(self, text="Recent", bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=18, pady=(12, 2))
        self.recent = tk.Label(self, bg=BG, fg=FG, font=("Consolas", 9), anchor="nw", justify="left", wraplength=520)
        self.recent.pack(fill="both", expand=True, padx=18)
        self._playlet = ("", 0.0)             # (answer, when asked): Playlet's own server only answers while it is on screen
        self._asking = False
        self.after(50, self.refresh)
        dark_titlebar(self)

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.app.health_report())
        self.copy_btn.style("Copied", ACCENT, ACCENT_HI)
        self.after(1600, lambda: self.copy_btn.winfo_exists() and self.copy_btn.style("Copy report", ACCENT, ACCENT_HI))

    def _ask_playlet(self):
        if self._asking or time.time() - self._playlet[1] < 5:
            return
        self._asking = True
        host = self.app._host

        def run():
            try:
                Remote._open(f"http://{host}:8888/api/state", timeout=2)
                answer = "answering"
            except Exception:
                answer = "not answering"
            self._playlet = (answer, time.time())
            self._asking = False
        threading.Thread(target=run, daemon=True).start()

    def _set(self, key, color, text):
        dot, _unused, label = self.rows[key]
        dot.set(color)
        label.config(text=text)

    def refresh(self):
        try:
            h = self.app.health.snapshot()
            self._ask_playlet()
            if h["tv_ok"] is None:
                self._set("tv", GREY, "Not checked yet")
            elif h["tv_ok"]:
                slow = h["tv_ms"] > 400
                self._set("tv", AMBER if slow else GREEN,
                          f"Answering in {h['tv_ms']:.0f} ms" + ("  ·  slow" if slow else "") +
                          (f"  ·  went quiet {h['tv_down']}x" if h["tv_down"] else ""))
            else:
                self._set("tv", RED, "Not answering. Is the TV on and on the same Wi-Fi?")
            app = h["tv_app"] or "nothing"
            playlet = self._playlet[0]
            self._set("playlet", GREEN if h["tv_app"] == "Playlet" else GREY,
                      f"On the TV now: {app} ({h['tv_state']})" + (f"  ·  its server is {playlet}" if playlet else ""))
            self._set("sync", AMBER if h["reloads"] > 4 else GREEN if self.app.bridge.connected() else GREY,
                      f"{h['reloads']} reloads, {h['reloads_skipped']} avoided" +
                      ("" if self.app.bridge.connected() else "  ·  YouTube tab not connected"))
            self._set("buffer", AMBER if h["buffers"] > 2 else GREEN,
                      f"{h['buffers']} times, {h['buffer_seconds']:.0f} s in all" if h["buffers"] else "None")
            st = h["stalls"]
            total = st["app"] + st["browser"] + st["link"] + st["unknown"]
            self._set("stalls", AMBER if total else GREEN,
                      "None" if not total else f"this app {st['app']}  ·  browser {st['browser']}  ·  "
                                               f"extension link {st['link']}  ·  unknown {st['unknown']}")
            self._set("audio", *self.app.audio_health())
            self.recent.config(text="\n".join(f"{ago:4.0f}s ago  {k:15s} {d}"[:110] for ago, k, d in h["recent"]) or
                               "Nothing has gone wrong in the last 30 minutes.")
        except Exception as e:
            log(f"health panel: {e!r}")
        self.after(1000, self.refresh)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CapoCast")
        self.configure(bg=BG)
        cfg = self._load()
        w, h = 1280, 800
        where = restore_geometry(cfg.get("window"), virtual_screen()) if virtual_screen() else None
        if where:
            self.geometry(f"{where[0]}x{where[1]}+{where[2]}+{where[3]}")
            self._normal_geo = where[:4]
        else:
            self.geometry(f"{w}x{h}+{max(0, (self.winfo_screenwidth() - w) // 2)}+{max(0, (self.winfo_screenheight() - h) // 2 - 20)}")
        self.minsize(MIN_W, MIN_H)
        self.bind("<Configure>", self._note_geometry, add="+")
        self.listener = None
        self.host = tk.StringVar(value=cfg.get("host", DEFAULT_HOST))
        self.delay = tk.StringVar(value=str(cfg.get("delay", 500)))
        self.buffer = tk.StringVar(value=str(cfg.get("buffer", BUFFER_MS)))
        self.status = tk.StringVar(value="Not connected")
        self.audio_device = cfg.get("audio_device", "")
        # worker threads read the Roku IP from here (Tk variables aren't thread-safe)
        self._host = self.host.get().strip() or DEFAULT_HOST
        self.host.trace_add("write", lambda *a: setattr(self, "_host", self.host.get().strip() or DEFAULT_HOST))
        try:
            self._icon = tk.PhotoImage(file=asset("capocast.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        self.autocast = cfg.get("autocast", True)
        # one Discord status for both tabs, each with its own switch (both off until you turn them on)
        self.discord = discord_presence.Status(cfg.get("discord_id", ""), log)
        self.discord.enable("web", cfg.get("discord_on", False))
        self.discord.enable("youtube", cfg.get("discord_youtube", False))
        self._yt_since, self._yt_gone = 0.0, 0
        self.sync = None   # created once the bridge exists
        self.bridge = Bridge(log)
        self.bridge.settings["autocast"] = self.autocast
        self.health = health.Health(log=log)
        self.bridge.on_poll = lambda gap, rtt: self.health.page_poll(gap, rtt)
        self.sync = CastSync(lambda: self._host, self.bridge, self._yt_now)
        self.sync.health = self.health
        self._watchdog()
        self.bridge.on_event = lambda ev: self.sync.push(ev) if self.autocast else None
        self.bridge_error = False
        try:
            self.bridge.start()
        except OSError as e:
            self.bridge_error = True
            log(f"bridge failed: {e!r}")
        self.chrome = yt_embed.ChromeEmbed(log)

        self._build_topbar()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        self.pages = {"Listen": tk.Frame(body, bg=BG), "YouTube": tk.Frame(body, bg=BG), "Web": tk.Frame(body, bg=BG)}
        self._build_listen(self.pages["Listen"])
        self._build_youtube(self.pages["YouTube"])
        self.phone_store = phone_store.PhoneStore(PHONE_STORE)
        ui = SimpleNamespace(BG=BG, CARD=CARD, EDGE=EDGE, FG=FG, MUTED=MUTED, ACCENT=ACCENT, ACCENT_HI=ACCENT_HI,
                             GREEN=GREEN, AMBER=AMBER, RED=RED, GREY=GREY, FONT=FONT, FONT_MED=FONT_MED,
                             RoundButton=RoundButton, NavButton=NavButton, Chip=Chip, Tooltip=Tooltip,
                             dark_titlebar=dark_titlebar, Remote=Remote, log=log)
        self.web = web_tab.WebTab(self, self.pages["Web"], ui, cfg.get("web_home"), cfg.get("web_upscale"), self.discord,
                                 subs=cfg.get("web_subs", ""), hdr=cfg.get("web_hdr", False))
        self.page = None
        self._show("YouTube" if "--youtube" in sys.argv else "Web" if "--web" in sys.argv else "Listen")

        # the phone remote: off until you turn it on in the Phone remote window, then remembered
        self.last_tv = None
        self.phone_win = None
        self.phone = phone_remote.PhoneRemote(PhoneBackend(self), cfg.get("phone_token") or phone_remote.new_token(),
                                              self._phone_icon(), log=log)
        if cfg.get("phone_on", False):
            self._phone_start()

        self.protocol("WM_DELETE_WINDOW", self.close)
        if where and where[4]:
            self.state("zoomed")
        if "--autoconnect" in sys.argv:
            self.after(500, self.connect)
        self.update()
        self._dark_titlebar()
        self._poll()
        self._yt_tick()
        self._yt_focus_tick()
        self._status_tick()

    # ---- layout ----------------------------------------------------------
    def _build_topbar(self):
        bar = tk.Canvas(self, height=BAR_H, bg=BG, highlightthickness=0)
        bar.pack(fill="x")
        self.bar = bar
        cy = BAR_H // 2

        # brand: the CapoCast mark + wordmark, sharing one centerline
        self._logo = ImageTk.PhotoImage(Image.open(asset("capocast.png")).resize((30, 30), Image.LANCZOS))
        bar.create_image(22, cy, image=self._logo, anchor="w")
        brand = (FONT, 13, "bold")
        bar.create_text(60, text_y(brand, cy), text="CapoCast", anchor="w", fill=FG, font=brand)

        self.tabs = TabSwitch(bar, ["Listen", "YouTube", "Web"], self._show, seg_w=88)
        bar.create_window(170, cy, window=self.tabs, anchor="w")

        # browser nav sits next to the tabs, like a browser's toolbar
        self.yt_items = []
        x = 170 + int(self.tabs["width"]) + 18
        for kind, cmd in (("back", lambda: self.bridge.send({"history": -1})),
                          ("fwd", lambda: self.bridge.send({"history": 1})),
                          ("reload", lambda: self.bridge.send({"reload": True}))):
            self.yt_items.append(bar.create_window(x, cy, window=NavButton(bar, kind, cmd), anchor="w"))
            x += 36
        self._bar_left = x + 16      # the status text may not run further left than this

        # far right, on every tab: the Phone remote and the Connection health panel (its dot shows the TV's reachability)
        self.phone_btn = NavButton(bar, "phone", self.open_phone)
        self.health_btn = NavButton(bar, "pulse", self.open_health)
        self.kb_btn = NavButton(bar, "keyboard", self.toggle_typebox)      # shows / hides the Type on TV box
        self.phone_item = bar.create_window(0, cy, window=self.phone_btn, anchor="e")
        self.health_item = bar.create_window(0, cy, window=self.health_btn, anchor="e")
        self.kb_item = bar.create_window(0, cy, window=self.kb_btn, anchor="e")
        Tooltip(self.kb_btn, "Type on TV: show or hide the typing box (search and sign-in boxes, Ctrl+V pastes)")
        Tooltip(self.phone_btn, "Phone remote: control the TV from your phone")
        Tooltip(self.health_btn, "Connection health: what stalled, and who froze")

        # then, on the YouTube tab: the Auto-cast and Discord switches, Resync, and the sync status
        label = (FONT_MED, 10)
        self.ic_div = bar.create_line(0, cy - 11, 0, cy + 11, fill=EDGE)
        self.auto_toggle = Toggle(bar, self.autocast, self._set_autocast)
        self.r_toggle = bar.create_window(0, cy, window=self.auto_toggle, anchor="e")
        self.r_label = bar.create_text(0, text_y(label, cy), text="Auto-cast", anchor="e", fill="#d4d4da", font=label)
        self.r_div = bar.create_line(0, cy - 11, 0, cy + 11, fill=EDGE)
        self.dc_toggle = Toggle(bar, self.discord.enabled.get("youtube", False), self._set_discord_youtube)
        self.dc_win = bar.create_window(0, cy, window=self.dc_toggle, anchor="e")
        self.dc_label = bar.create_text(0, text_y(label, cy), text="Discord", anchor="e", fill="#d4d4da", font=label)
        self.dc_div = bar.create_line(0, cy - 11, 0, cy + 11, fill=EDGE)
        self.resync_chip = Chip(bar, "Resync", self._resync)
        self._rs_text = "Resync"
        self.rs_win = bar.create_window(0, cy, window=self.resync_chip, anchor="e")
        status = (FONT, 9)
        self._status_text, self._status_font = "", status
        self.r_status = bar.create_text(0, text_y(status, cy), text="", anchor="e", fill=MUTED, font=status)
        self.r_dot = bar.create_image(0, cy, image=dot_picture(bar, 8, GREY), anchor="e")
        Tooltip(self.auto_toggle, "Auto-cast: whatever you play in the YouTube tab also plays on the TV")
        Tooltip(self.dc_toggle, "Show what you're watching on Discord")
        Tooltip(self.resync_chip, "Line the video up on this PC and the TV again, and match play / pause")
        self.yt_items += [self.ic_div, self.r_toggle, self.r_label, self.r_div, self.dc_win, self.dc_label, self.dc_div,
                          self.rs_win, self.r_status, self.r_dot]
        self.hairline = bar.create_line(0, BAR_H - 1, 0, BAR_H - 1, fill=EDGE)
        bar.bind("<Configure>", lambda e: self._layout_bar(e.width))

    def _layout_bar(self, width=None):
        bar, cy = self.bar, BAR_H // 2
        width = width or bar.winfo_width()
        bar.coords(self.hairline, 0, BAR_H - 1, width, BAR_H - 1)
        x = width - 20
        bar.coords(self.phone_item, x, cy)
        x -= int(self.phone_btn["width"]) + 2
        bar.coords(self.health_item, x, cy)
        x -= int(self.health_btn["width"]) + 2
        bar.coords(self.kb_item, x, cy)
        x -= int(self.kb_btn["width"]) + 14
        bar.coords(self.ic_div, x, cy - 11, x, cy + 11)
        x -= 16
        bar.coords(self.r_toggle, x, cy)
        x -= int(self.auto_toggle["width"]) + 10
        bar.coords(self.r_label, x, bar.coords(self.r_label)[1])
        x -= tkfont.Font(font=bar.itemcget(self.r_label, "font")).measure("Auto-cast") + 16
        bar.coords(self.r_div, x, cy - 11, x, cy + 11)
        x -= 16
        bar.coords(self.dc_win, x, cy)
        x -= int(self.dc_toggle["width"]) + 10
        bar.coords(self.dc_label, x, bar.coords(self.dc_label)[1])
        x -= tkfont.Font(font=bar.itemcget(self.dc_label, "font")).measure("Discord") + 16
        bar.coords(self.dc_div, x, cy - 11, x, cy + 11)
        x -= 16
        bar.coords(self.rs_win, x, cy)
        x -= int(self.resync_chip["width"]) + 18
        # the status text takes what room is left (so a long title is shortened, never run into the nav buttons)
        text = fit_text(self._status_font, self._status_text, x - self._bar_left - 16)
        bar.itemconfigure(self.r_status, text=text)
        bar.coords(self.r_status, x, bar.coords(self.r_status)[1])
        x -= tkfont.Font(font=self._status_font).measure(text) + 8
        bar.coords(self.r_dot, x, cy)

    def _set_sync_status(self, text, color):
        self._status_text = text
        self.bar.itemconfigure(self.r_dot, image=dot_picture(self.bar, 8, color))
        self._layout_bar()

    def _status_tick(self):
        """Top-bar sync status: extension connection + what's on the TV."""
        sn = self.sync
        want = "Syncing…" if sn.syncing else "Resync"
        if want != self._rs_text:
            self._rs_text = want
            self.resync_chip.config(text=want)
        if self.bridge_error:
            self._set_sync_status("Port 8765 is busy - close other copies of CapoCast", RED)
        elif sn.note and time.time() < sn.note_until:       # what the Resync button just did
            self._set_sync_status(sn.note, GREEN if sn.note_ok else AMBER)
        elif not self.bridge.connected():
            if self.chrome.waiting():
                self._set_sync_status("Starting YouTube…", AMBER)
            elif not self.chrome.alive():
                self._set_sync_status("YouTube isn't open", GREY)
            elif time.time() - getattr(self, "_embedded_at", 0) < 10:
                self._set_sync_status("Connecting…", AMBER)
            else:
                self._set_sync_status("Reload Playlet Cast in chrome://extensions", GREY)
        elif not self.autocast:
            self._set_sync_status("Auto-cast off", GREY)
        elif sn.error:
            self._set_sync_status(sn.error, RED)
        elif sn.title:
            self._set_sync_status("On TV  ·  " + sn.title, GREEN)
        else:
            self._set_sync_status("Ready  ·  pick a video", GREEN)
        self.after(700, self._status_tick)

    def _resync(self):
        """The Resync button: line the PC's player and the TV up again, whichever way they drifted."""
        if not self.autocast:
            return self.sync._say("Turn Auto-cast on first: it is what links this player to the TV", False)
        if not self.bridge.connected():
            return self.sync._say("The YouTube tab isn't linked to CapoCast yet", False)
        if self.sync.request_resync():
            self._rs_text = "Syncing…"
            self.resync_chip.config(text="Syncing…")
            self._layout_bar()

    def _build_listen(self, page):
        wrap = tk.Frame(page, bg=BG)
        wrap.place(relx=0.5, rely=0.47, anchor="center")
        col = tk.Frame(wrap, bg=BG)
        col.pack(side="left", anchor="n")
        tk.Label(col, text="TV audio", bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(col, text="Roku private listening, on this PC", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(0, 14))

        card = tk.Frame(col, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        card.pack(fill="x")
        srow = tk.Frame(card, bg=CARD)
        srow.pack(fill="x", padx=16, pady=14)
        self.dot = Dot(srow, 12, CARD)
        self.dot.pack(side="left")
        tk.Label(srow, textvariable=self.status, bg=CARD, fg=FG, font=(FONT, 10), anchor="w",
                 wraplength=270, justify="left").pack(side="left", padx=(10, 0))

        self.btn = RoundButton(col, "Connect", self.toggle)
        self.btn.pack(pady=(16, 18))

        # transport: acts on whatever the Roku is playing, independent of the audio connection
        trow = tk.Frame(col, bg=BG)
        trow.pack(fill="x")
        self.play_btn = IconButton(trow, "play", self._toggle_play, width=48)
        for i, (kind, action) in enumerate([
            ("back", lambda: self.keys.key("Back")),      # through the key queue, so it lands after any typing
            ("back10", lambda: self._remote(Remote.seek, -10)),
            ("rev", lambda: self._remote(Remote.key, "Rev")),
            (None, None),
            ("fwd", lambda: self._remote(Remote.key, "Fwd")),
            ("fwd10", lambda: self._remote(Remote.seek, 10)),
        ]):
            btn = self.play_btn if kind is None else IconButton(trow, kind, action, width=48)
            btn.pack(side="left", padx=(0 if i == 0 else 8, 0))   # 6 x 48 + 5 x 8 = 328, the column width
        self.now = tk.Label(col, text="Nothing playing", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.now.pack(fill="x", pady=(8, 2))
        self.progress = ProgressBar(col, self._seek_fraction)      # how far in; click to jump (YouTube in Playlet)
        self.progress.pack(fill="x", pady=(0, 8))
        self.play_state, self._polling = "none", False

        self.type_wrap = tk.Frame(col, bg=BG)       # packed only while the keyboard icon is on (see toggle_typebox)
        self._type_visible, self._type_auto = False, False
        tk.Label(self.type_wrap, text="Type on TV", bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        self.keys = KeySender(lambda: self._host)

        # Remote column beside it (stacked below, it wouldn't fit the minimum window height). Same heading and
        # subtitle as "TV audio", so both columns share one top edge and the D-pad lines up with the status card.
        side = tk.Frame(wrap, bg=BG)
        side.pack(side="left", anchor="n", padx=(64, 0))
        tk.Label(side, text="Remote", bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(side, text="Arrows and OK, like the Roku remote", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(0, 14))
        self.dpad = DPad(side, self.keys.key)
        self.dpad.pack(anchor="w")
        tk.Label(side, text="Hold an arrow to repeat", bg=BG, fg=MUTED, font=(FONT, 9),
                 anchor="w").pack(fill="x", pady=(8, 0))   # same spacing as the "now playing" line opposite
        tk.Label(side, text="Apps", bg=BG, fg=MUTED, font=(FONT, 10), anchor="w").pack(fill="x", pady=(14, 6))
        self.apps_row = AppRow(side, tv_apps.clean_pins(self._load().get("quick_apps")), self._set_pins,
                               lambda: self._host, log)
        self.apps_row.pack(anchor="w")
        self.typebox = TypeBox(self.type_wrap, self.keys)
        self.typebox.pack(pady=(4, 16))
        # clicking anywhere else (except a text field) takes keyboard focus away from the Type box
        self.bind_all("<Button-1>", self._click_away, add="+")
        # typing with nothing else selected goes to the TV too (bound on the window, so Entries are left alone)
        self.bind("<KeyPress>", self._stray_key)
        self._kb_from = None   # tab to go back to when the TV's keyboard closes
        self.tvvol = TvVolume(lambda: self._host, lambda: self._after_safe(self._tv_volume_changed))
        self.kb_watch = KeyboardWatch(lambda: self._host, self._on_tv_keyboard, self.tvvol.heard)

        # One volume control for whichever place the sound is going: this PC while audio is routed here (Connect),
        # the TV's own volume otherwise. It switches by itself when you connect or disconnect.
        vrow = self.vol_row = tk.Frame(col, bg=BG)
        vrow.pack(fill="x")
        self.vol_title = tk.Label(vrow, text="Volume", bg=BG, fg=MUTED, font=(FONT, 10))
        self.vol_title.pack(side="left")
        self.vol_label = tk.Label(vrow, bg=BG, fg=FG, font=(FONT, 10, "bold"))
        self.vol_label.pack(side="right")
        self.mute_lbl = tk.Label(vrow, text="Mute", bg=BG, fg=MUTED, font=(FONT, 9), cursor="hand2")
        self.mute_lbl.bind("<Button-1>", lambda e: self.tvvol.toggle_mute())
        self.pc_volume = int(self._load().get("volume", 100))
        self.scale = Slider(col, 0, 200, self.pc_volume, self._on_volume)
        self.scale.pack(pady=(4, 10))
        self._refresh_volume()

        # Lip-sync: how late the sound plays, live. Drag it while watching until the lips match.
        srow = tk.Frame(col, bg=BG)
        srow.pack(fill="x")
        tk.Label(srow, text="Sound delay", bg=BG, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self.sound_label = tk.Label(srow, bg=BG, fg=FG, font=(FONT, 10, "bold"))
        self.sound_label.pack(side="right")
        self.sound_scale = Slider(col, 0, SOUND_DELAY_MAX, self._buffer(), self._on_sound_delay)
        self.sound_scale.mark = None
        self.sound_scale.draw()                  # it drew itself once already, with the 100% tick this slider doesn't have
        self.sound_scale.pack(pady=(4, 10))
        self.sound_label.config(text=f"{self._buffer()} ms")
        Tooltip(self.sound_scale, "How late the sound plays on this PC. Drag it while watching until the lips match.")

        orow = tk.Frame(col, bg=BG)
        orow.pack(fill="x")
        self.host_entry = self._field(orow, "Roku IP", self.host, 15)
        self.delay_entry = self._field(orow, "Roku buffer", self.delay, 6, pad=(14, 0))
        Tooltip(self.delay_entry, "Milliseconds of sound the Roku holds back before sending it here. Higher = later "
                                  "sound and fewer crackles. Applies the next time you press Connect.")
        find = tk.Label(self.host_entry.master, text="Find", bg=BG, fg=ACCENT_HI, font=(FONT, 9, "bold"), cursor="hand2")
        find.place(relx=1.0, y=0, anchor="ne")
        find.bind("<Button-1>", lambda e: self._find_roku())
        Tooltip(find, "Look for a Roku on your network, so you don't have to type its address")
        obox = tk.Frame(orow, bg=BG)
        obox.pack(side="left", padx=(14, 0), fill="x", expand=True)
        tk.Label(obox, text="Output", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        self.out_lbl = tk.Label(obox, bg=CARD, fg=FG, font=(FONT, 10), anchor="w", cursor="hand2", padx=8,
                                highlightthickness=1, highlightbackground=EDGE)
        self.out_lbl.pack(fill="x", ipady=5, pady=(3, 0))
        self.out_lbl.bind("<Button-1>", self._pick_output)
        self._paint_output()
        self.buffer_hint = tk.Label(col, text="", bg=BG, fg=AMBER, font=(FONT, 9), anchor="w")
        self._paint_buffer_hint()

    def _build_youtube(self, page):
        self.yt_page = page
        self.yt_msg = tk.Label(page, text="", bg=BG, fg=MUTED, font=(FONT, 11), cursor="")
        self.yt_msg.bind("<Button-1>", lambda e: self._yt_reopen())
        page.bind("<Configure>", lambda e: self.chrome.fit(e.width, e.height))

    # ---- tabs ------------------------------------------------------------
    def _show(self, name):
        if name == self.page:
            return
        if self.page:
            self.pages[self.page].pack_forget()
        self.page = name
        self.tabs.set(name)
        self.pages[name].pack(fill="both", expand=True)
        for item in self.yt_items:
            self.bar.itemconfigure(item, state="normal" if name == "YouTube" else "hidden")
        if name == "YouTube":
            if self.chrome.alive():
                self.chrome.show(True)
                self.update_idletasks()
                self.chrome.fit(self.yt_page.winfo_width(), self.yt_page.winfo_height())
                self.chrome.focus()
            elif not self.chrome.waiting():
                self._yt_reopen()
        else:
            self._hide_chrome()
        # each tab hides the other's browser; the Web tab starts its own the first time it's opened
        if name == "Web":
            self.web.on_show()
        else:
            self.web.on_hide()

    # ---- YouTube (embedded Chrome) ----------------------------------------
    def _yt_message(self, text, clickable=False):
        self.yt_msg.config(text=text, fg=FG if clickable else MUTED, cursor="hand2" if clickable else "")
        if text:
            self.yt_msg.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.yt_msg.place_forget()    # an empty label still sat on top of the page, over a few pixels of the video

    def _yt_reopen(self, url=None):
        if self.chrome.alive() or self.chrome.waiting():
            return
        self._yt_message("Opening YouTube…")
        try:
            self.chrome.launch(url)
        except Exception as e:
            self._yt_message(f"{e}  ·  click to retry", clickable=True)

    # ---- keyboard focus for the embedded Chrome -----------------------------
    FOCUS_EVERY = 80       # ms between checks that the keyboard is where the user is looking

    def _app_in_front(self):
        try:
            return ctypes.windll.user32.GetForegroundWindow() == ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)
        except Exception:
            return False

    def _hide_chrome(self):
        """Another tab is showing: hide Chrome, and take the keyboard back if it had it (a hidden Chrome would keep it,
        and the other tabs would hear nothing)."""
        had_keys = self.chrome.has_focus()
        self.chrome.show(False)
        if had_keys:
            self._focus_tk()

    def _focus_tk(self):
        """Give the keyboard back to the app's own window (SetFocus directly: Tk believes it already has it, so
        focus_set would do nothing)."""
        try:
            if self._app_in_front():
                ctypes.windll.user32.SetFocus(self.winfo_id())
        except Exception as e:
            log(f"focus error: {e!r}")

    def _yt_focus_tick(self):
        """While the YouTube tab is showing and the app is in front, keep the keyboard in Chrome. Clicking into the
        embedded page doesn't move it there by itself, and Tk takes it back whenever the window is re-activated, so the
        search box looked frozen."""
        try:
            if self.page == "YouTube" and self.chrome.alive() and self._app_in_front():
                if time.time() >= getattr(self, "_yt_focus_retry", 0.0) and self.chrome.keep_focus():
                    if self.chrome.has_focus():
                        self._yt_focus_retry = 0.0
                    else:                       # Chrome sent it somewhere else again: don't fight it every tick
                        self._yt_focus_retry = time.time() + 1.0
                        if time.time() - getattr(self, "_yt_focus_logged", 0.0) > 30:
                            self._yt_focus_logged = time.time()
                            log("youtube tab: Chrome would not take the keyboard focus")
        except Exception as e:
            log(f"focus error: {e!r}")
        self.after(self.FOCUS_EVERY, self._yt_focus_tick)

    def _yt_tick(self):
        try:
            if self.chrome.waiting():
                hwnd = self.chrome.find_new()
                if hwnd:
                    self.chrome.embed(hwnd, self.yt_page.winfo_id())
                    self._embedded_at = time.time()
                    self.update_idletasks()
                    self.chrome.fit(self.yt_page.winfo_width(), self.yt_page.winfo_height())
                    self.chrome.show(self.page == "YouTube")
                    self._yt_message("")
                elif self.chrome.timed_out():
                    self.chrome._before = None
                    self._yt_message("Couldn't open YouTube  ·  click to retry", clickable=True)
            elif self.chrome.hwnd and not self.chrome.alive():
                self.chrome.hwnd = None
                self._yt_message("YouTube was closed  ·  click to reopen", clickable=True)
        except Exception as e:
            log(f"embed error: {e!r}")
        self.after(250, self._yt_tick)

    def _set_autocast(self, on):
        self.autocast = on
        self.bridge.settings["autocast"] = on
        if not on:
            self.discord.clear("youtube")     # the TV is no longer being driven from here
        self._save()

    # ---- Discord status for YouTube videos ---------------------------------
    def _set_discord_youtube(self, on):
        if on and not self.discord.client_id:
            found, problem = discord_presence.ask_application_id(self)
            if not found:
                self.dc_toggle.on = False
                self.dc_toggle.draw()
                if problem:
                    messagebox.showinfo("Discord status", problem, parent=self)
                return
            self.discord.set_id(found)
        self.discord.enable("youtube", on)
        if on and self.sync.current:
            self._yt_now(self.sync.title, self.sync.current)
        self._save()

    def _yt_now(self, title, video_id):
        """A video started on the TV (or ended). Called from CastSync's thread; Status is safe to use from any thread."""
        self._yt_want = video_id
        if not video_id:
            self.discord.clear("youtube")
            return
        if video_id != getattr(self, "_yt_id", None):
            self._yt_id, self._yt_since = video_id, time.time()
        if not self.discord.is_on("youtube"):
            return
        threading.Thread(target=self._yt_show, args=(title, video_id), daemon=True).start()

    def _yt_show(self, title, video_id):
        """Channel on the first line, the video's title under it. Both are looked up by video id, because the page
        title the extension sends is still the previous video's when a new one starts."""
        channel, name = discord_presence.youtube_info(video_id)
        if self._yt_want != video_id:
            return                        # the video changed (or ended) while this was being looked up
        self.discord.show("youtube", channel or "YouTube", name or title or video_id, self._yt_since,
                          f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", image_text=name or title)

    def on_tv_state(self, st):
        """From the 1 s poll: if the TV has left Playlet, the YouTube status comes down. If it is playing something the
        status doesn't know about (started from the phone), that goes up."""
        if st and st["app"] == "Playlet" and st["state"] in ("play", "pause", "buffer") and self.discord.is_on("youtube"):
            self._adopt_playlet()
        if self.discord.showing != "youtube" or st is None:
            self._yt_gone = 0
            return
        gone = st["state"] == "none" or st.get("app") != "Playlet"
        if not gone and st["state"] in ("play", "pause"):
            self.discord.progress("youtube", st["pos"], st["dur"], st["state"] == "play")      # the clock follows the video
        self._yt_gone = self._yt_gone + 1 if gone else 0
        if self._yt_gone >= 2:
            self._yt_gone = 0
            self.discord.clear("youtube")

    ADOPT_EVERY = 5.0            # seconds between looks at what Playlet is playing

    def _adopt_playlet(self):
        """A video can reach Playlet without the YouTube tab: the phone's YouTube tab, a YouTube link sent from the phone,
        or someone using the TV's remote. CastSync only hears about what the Chrome extension sends, so those never got a
        Discord status. Every few seconds, while the TV is in Playlet, ask it what's on and show that if it isn't up."""
        now = time.time()
        if now - getattr(self, "_yt_adopt_at", 0.0) < self.ADOPT_EVERY:
            return
        self._yt_adopt_at = now
        threading.Thread(target=self._yt_adopt, daemon=True).start()

    def _yt_adopt(self):
        np = Remote.playlet_now(self._host)               # {} if Playlet isn't answering
        vid = np.get("videoid") or ""
        if vid and (vid != getattr(self, "_yt_id", None) or self.discord.showing != "youtube"):
            log(f"discord: Playlet is playing {vid}, which the YouTube tab didn't start; showing it")
            self._yt_now(np.get("title") or "", vid)

    # ---- watching on the PC as well as the TV (from the phone's "Also watch on the PC") -------------------
    def watch_youtube_on_pc(self, video_id, title, pos):
        """Open the video the TV is playing in the YouTube tab, `pos` seconds in. Runs on the Tk thread. With Auto-cast on
        the page then follows the TV (muted, nudged onto the TV's timeline) like any video started here."""
        url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id, safe='')}&t={int(max(0, pos))}s"
        self.sync.adopt(video_id, title)
        if self.chrome.alive():
            self.bridge.send({"navigate": url})
        elif not self.chrome.waiting():
            self._yt_reopen(url)
        else:                                 # still opening: it picks the page up on its first poll
            self.bridge.send({"navigate": url})
        self._show("YouTube")
        self._pop_up()

    def show_web_on_pc(self):
        """The Web tab's browser is already on the page being cast; this just brings it up. Runs on the Tk thread."""
        self._show("Web")
        self._pop_up()

    def autocast_off(self):
        """A Web-tab cast owns the TV now; stop the YouTube tab's auto-cast from taking it back."""
        if self.autocast:
            self.auto_toggle.on = False
            self.auto_toggle.draw()
            self._set_autocast(False)

    # ---- transport -------------------------------------------------------
    def _remote(self, fn, *args):
        """Run a Roku call off the UI thread, then refresh the readout."""
        host = self.host.get().strip() or DEFAULT_HOST

        def work():
            try:
                fn(host, *args)
            except Exception as e:
                log(f"remote error: {e!r}")
            self._fetch(host)
        threading.Thread(target=work, daemon=True).start()

    def _seek_fraction(self, frac):
        st = self.last_tv
        if st and st["dur"] and st["app"] == "Playlet":
            self.progress.set(frac, True)
            self._remote(Remote.seek_to, frac * st["dur"])

    def _find_roku(self):
        """Look for Rokus on the network (off the UI thread) and put the address in the box."""
        self.apps_row.say("Looking for your Roku…")

        def work():
            found = roku_find.search()
            self._after_safe(lambda: self._found_roku(found))
        threading.Thread(target=work, daemon=True).start()

    def _found_roku(self, found):
        if not found:
            return self.apps_row.say("No Roku answered. Is it on, and on this Wi-Fi?", AMBER, 6)
        if len(found) == 1 or self.listener:
            return self._use_roku(found[0], len(found))
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#ffffff", bd=0)
        for ip in found:
            menu.add_command(label=ip, command=lambda ip=ip: self._use_roku(ip, len(found)))
        try:
            menu.tk_popup(self.host_entry.winfo_rootx(), self.host_entry.winfo_rooty() + self.host_entry.winfo_height())
        finally:
            menu.grab_release()

    def _use_roku(self, ip, count):
        if self.listener:
            return self.apps_row.say(f"Found a Roku at {ip}. Disconnect first to switch to it.", AMBER, 6)
        self.host.set(ip)
        self._save()
        self.apps_row.say(f"Found a Roku at {ip}" + (f" (and {count - 1} more)" if count > 1 else ""), GREEN, 6)

    def _toggle_play(self):
        self.play_state = "pause" if self.play_state == "play" else "play"   # optimistic; next poll corrects it
        self.play_btn.set_kind("pause" if self.play_state == "play" else "play")
        self._remote(Remote.key, "Play")

    def _watchdog(self):
        """Notices when this app itself freezes (a thread that sleeps a quarter second and wakes much later was starved),
        so a stall seen from the YouTube page can be blamed on the right side."""
        def run():
            while True:
                t = time.monotonic()
                time.sleep(0.25)
                late = time.monotonic() - t - 0.25
                if late >= health.APP_LATE:
                    self.health.app_late(late)
        threading.Thread(target=run, name="watchdog", daemon=True).start()

    def _poll(self):
        now = time.monotonic()
        due = getattr(self, "_poll_due", None)
        if due is not None and now - due >= health.APP_LATE:      # the window's own loop was kept waiting
            self.health.app_late(now - due)
        self._poll_due = now + 1.0
        if not self._polling:
            self._polling = True
            host = self.host.get().strip() or DEFAULT_HOST
            threading.Thread(target=self._fetch, args=(host,), daemon=True).start()
        self.apps_row.tick(self.host.get().strip() or DEFAULT_HOST)
        self.after(1000, self._poll)

    def _fetch(self, host):
        t0 = time.monotonic()
        st = Remote.state(host)
        self.health.tv_poll(st is not None, (time.monotonic() - t0) * 1000)
        if st:
            self.health.tv_playing(st["state"], st["app"])
        try:
            self.after(0, self._show_state, st)
        except RuntimeError:  # window already closed
            pass

    def _show_state(self, st):
        self._polling = False
        self.last_tv = st
        fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
        if not st:
            self.play_state = "none"
            self.now.config(text="Can't reach Roku remote control")
        elif st["state"] == "none":
            self.play_state = "none"
            self.now.config(text="Nothing playing")
        else:
            self.play_state = st["state"]
            label = st["state"].capitalize() if st["state"] != "play" else "Playing"
            time_txt = f"  ·  {fmt(st['pos'])} / {fmt(st['dur'])}" if st["dur"] else ""
            self.now.config(text=f"{st['app'] or 'Roku'}  ·  {label}{time_txt}")
        self.play_btn.set_kind("pause" if self.play_state == "play" else "play")
        if st and st["state"] != "none" and st["dur"]:
            self.progress.set(st["pos"] / st["dur"], st["app"] == "Playlet")
        else:
            self.progress.set(0.0, False)
        health_btn = getattr(self, "health_btn", None)
        if health_btn is not None and health_btn.badge != (GREEN if st else RED):   # the pulse icon's dot: is the TV there?
            health_btn.badge = GREEN if st else RED
            health_btn.draw()
        self.web.tv_state(st)
        self.on_tv_state(st)
        self.typebox.render()   # picks up typing errors reported by the sender thread

    def _focused(self):
        try:
            return self.focus_get()
        except KeyError:   # focus is inside the embedded Chrome
            return None

    def _click_away(self, e):
        w = e.widget
        if isinstance(w, tk.Misc) and not self.typebox.owns(w) and not isinstance(w, tk.Entry) \
                and self._focused() is self.typebox:
            self.focus_set()

    def _stray_key(self, e):
        """A key pressed on the Listen tab with nothing selected: hand it to the Type box."""
        if e.widget is self and self.page == "Listen" and self._type_visible and self.typebox.wants(e):
            self.typebox.focus_set()
            result = self.typebox._key(e)
            self.after(1, self.typebox.render)   # draw the caret once focus has actually moved
            return result
        return None

    # ---- TV keyboard (pops up like the phone app's) ------------------------
    def _on_tv_keyboard(self, state):
        try:
            self.after(0, self._tv_keyboard, state)   # from the watcher thread: hop to the Tk thread
        except RuntimeError:   # window already closed
            pass

    def toggle_typebox(self, on=None):
        """The keyboard icon: show or hide the Type on TV box. Turning it on from another tab brings up the Listen tab,
        where the box is, and puts the cursor in it."""
        on = (not self._type_visible) if on is None else on
        self._type_auto = False
        if on == self._type_visible:
            return
        self._type_visible = on
        self.kb_btn.active = on
        self.kb_btn.draw()
        if on:
            self.type_wrap.pack(fill="x", before=self.vol_row)
            self._show("Listen")
            self.update_idletasks()
            self.typebox.focus_set()
            self.after(1, self.typebox.render)
        else:
            if self._focused() is self.typebox:
                self.focus_set()
            self.type_wrap.pack_forget()

    def _tv_keyboard(self, state):
        was_open = self.typebox.tv is not None
        self.typebox.set_tv(state)
        if state is not None and not was_open:
            log("TV keyboard opened")
            self._kb_from = self.page if self.page != "Listen" else None
            auto = not self._type_visible
            self.toggle_typebox(True)             # the TV is asking for text: the box has to be there
            self._type_auto = auto                # ...and goes away again by itself if the user hadn't opened it
            self._show("Listen")
            self.typebox.focus_set()
            self.after(1, self.typebox.render)
            self._pop_up()
        elif state is None and was_open:
            log("TV keyboard closed")
            if self._focused() is self.typebox:
                self.focus_set()
            if self._type_auto:
                self.toggle_typebox(False)
            if self._kb_from:
                self._show(self._kb_from)
            self._kb_from = None

    def _pop_up(self):
        """Bring the window forward. If another app (say, a game) has the foreground, Windows refuses and
        flashes the taskbar button instead, so this never yanks you out of what you're doing."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if self.state() == "iconic":
                ctypes.windll.user32.FlashWindow(hwnd, True)
            else:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as e:
            log(f"pop up failed: {e!r}")

    def _field(self, parent, label, var, width, pad=(0, 0)):
        box = tk.Frame(parent, bg=BG)
        box.pack(side="left", padx=pad)
        tk.Label(box, text=label, bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        e = tk.Entry(box, textvariable=var, width=width, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 10), highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT,
                     disabledbackground=BG, disabledforeground=MUTED)
        e.pack(ipady=5, ipadx=4, pady=(3, 0))
        return e

    def _dark_titlebar(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 build variants)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
            # nudge a repaint so the title bar picks it up
            self.withdraw()
            self.deiconify()
        except Exception:
            pass

    # ---- helpers ---------------------------------------------------------
    def _note_geometry(self, e):
        """Remember where the window sits while it is a normal window (a maximized one remembers where it came from)."""
        if e.widget is self and self.state() == "normal":
            m = re.match(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", self.geometry())
            if m:
                self._normal_geo = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))

    def _window_state(self):
        geo = getattr(self, "_normal_geo", None)
        if not geo:
            return None
        return {"w": geo[0], "h": geo[1], "x": geo[2], "y": geo[3], "zoomed": self.state() == "zoomed"}

    @staticmethod
    def _load():
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"host": self.host.get().strip(), "delay": self._delay(), "buffer": self._buffer(),
                           "volume": self.pc_volume, "window": self._window_state(),
                           "autocast": self.autocast, "audio_device": getattr(self, "audio_device", ""),
                           "quick_apps": getattr(getattr(self, "apps_row", None), "pins", None),
                           "web_home": getattr(getattr(self, "web", None), "home", None),
                           "web_upscale": getattr(getattr(self, "web", None), "upscale", "off"),
                           "web_subs": getattr(getattr(self, "web", None), "subs", ""),
                           "web_hdr": getattr(getattr(self, "web", None), "hdr", False),
                           "discord_id": self.discord.client_id,
                           "discord_on": self.discord.enabled.get("web", False),
                           "discord_youtube": self.discord.enabled.get("youtube", False),
                           "phone_on": self.phone.running if hasattr(self, "phone") else False,
                           "phone_token": self.phone.token if hasattr(self, "phone") else ""}, f)
        except OSError:
            pass

    # ---- phone remote ----------------------------------------------------------
    @staticmethod
    def _phone_icon():
        try:
            with open(asset("capocast.png"), "rb") as f:
                return f.read()
        except OSError:
            return b""

    def _phone_start(self):
        try:
            self.phone.start()
            return True
        except OSError as e:
            log(f"phone remote failed to start: {e!r}")
            return False

    def _phone_pc_volume(self, delta):
        """The phone's volume buttons while the sound is playing on this PC: nudge the same gain the slider controls."""
        if not self.listener:
            return
        self.pc_volume = max(0, min(200, self.pc_volume + delta))
        self.listener.volume = self.pc_volume / 100
        self.scale.set(self.pc_volume)
        self._refresh_volume()

    def _phone_link(self):
        try:
            return self.phone.link(local_ip_for(self._host))
        except OSError:
            return self.phone.link("127.0.0.1")

    def open_health(self):
        win = getattr(self, "_health_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        self._health_win = HealthWindow(self)

    def audio_health(self):
        """(colour, text) for the panel's Audio row."""
        ln = self.listener
        if not ln:
            return GREY, "Not connected"
        st, buf = ln.stats, ln.buffer
        if not st or not buf:
            return AMBER, "Starting…"
        text = (f"this minute: {st.lost} packets lost in {st.gaps} gaps, {st.concealed} concealed  ·  buffer "
                f"{buf.level() * 1000:.0f} of {buf.target * 1000:.0f} ms  ·  {buf.underruns} underruns, {buf.dropped} dropped")
        if not ln.concealing:
            text += "  ·  no libopus: losses are not concealed"
        return (AMBER if buf.underruns else GREEN), text

    def health_report(self):
        extra = [f"Audio: {self.audio_health()[1]}", f"Last audio minute: {self.listener.last_report or '-'}" if self.listener else "",
                 f"YouTube tab connected: {self.bridge.connected()}", f"CapoCast extension polls seen: {self.health.polls}"]
        return self.health.report(extra)

    def open_phone(self):
        """The Phone remote window: the on/off switch, the link as a QR code, and how to put it on the home screen."""
        if self.phone_win and self.phone_win.winfo_exists():
            self.phone_win.lift()
            return
        import segno
        win = self.phone_win = tk.Toplevel(self)
        win.title("Phone remote")
        dark_titlebar(win)
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(False, False)
        tk.Label(win, text="Phone remote", bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(anchor="w", padx=24, pady=(20, 2))
        tk.Label(win, text="Control the TV from your phone on the same Wi-Fi.\nFree, and no app store.", bg=BG, fg=MUTED,
                 font=(FONT, 10), justify="left").pack(anchor="w", padx=24)
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=24, pady=(14, 10))
        state_var, link_var = tk.StringVar(), tk.StringVar()
        tk.Label(top, textvariable=state_var, bg=BG, fg=FG, font=(FONT, 10, "bold")).pack(side="left")
        entry = tk.Entry(win, textvariable=link_var, state="readonly", readonlybackground=CARD, fg=FG, relief="flat",
                         font=(FONT, 9), highlightthickness=1, highlightbackground=EDGE)
        qr_label = tk.Label(win, bg="#ffffff")
        hint = tk.Label(win, bg=BG, fg=MUTED, font=(FONT, 10), justify="left", wraplength=340, anchor="w")

        def paint():
            if self.phone.running:
                state_var.set("On")
                link = self._phone_link()
                link_var.set(link)
                m = segno.make(link, error="m").matrix
                n = len(m)
                img = Image.new("L", (n + 8, n + 8), 255)             # 4-module quiet zone around the code
                for y, row in enumerate(m):
                    for x, v in enumerate(row):
                        if v:
                            img.putpixel((x + 4, y + 4), 0)
                self._phone_qr = ImageTk.PhotoImage(img.resize((6 * (n + 8),) * 2, Image.NEAREST))
                qr_label.config(image=self._phone_qr)
                qr_label.pack(before=entry, pady=(0, 10))
                hint.config(text="1. Scan the code with your iPhone's camera and open the link in Safari.\n"
                                 "2. Tap Share, then Add to Home Screen.\n"
                                 "3. Open CapoCast from your home screen.\n\n"
                                 "If Windows asks, allow CapoCast on private networks.")
            else:
                state_var.set("Off")
                link_var.set("")
                qr_label.pack_forget()
                hint.config(text="Turn it on to get a code to scan. Only devices on your home network can reach it, "
                                 "and only with the link in the code.")

        def flip(on):
            if on:
                if not self._phone_start():
                    messagebox.showerror("Phone remote", "Couldn't start the phone remote: the network port is busy.",
                                         parent=win)
            else:
                self.phone.stop()
            paint()
            self._save()

        def reset(_e=None):
            if not messagebox.askyesno("New link", "Make a new link? Phones using the old one stop working until they "
                                       "scan the new code.", parent=win):
                return
            self.phone.token = phone_remote.new_token()
            paint()
            self._save()

        Toggle(top, self.phone.running, flip).pack(side="right")
        entry.pack(fill="x", padx=24, ipady=5)
        hint.pack(fill="x", padx=24, pady=(12, 6))
        new = tk.Label(win, text="Make a new link", bg=BG, fg=MUTED, font=(FONT, 9, "underline"), cursor="hand2")
        new.pack(anchor="w", padx=24, pady=(0, 18))
        new.bind("<Button-1>", reset)
        paint()

    def _set_pins(self, pins):
        self._save()

    def _paint_output(self):
        name = self.audio_device or "System default"
        self.out_lbl.config(text=name if len(name) <= 16 else name[:15] + "…")

    def _pick_output(self, e=None):
        """Menu of sound outputs. Takes effect at once while connected."""
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#ffffff", bd=0)
        for label, name in [("System default", "")] + [(n, n) for n in output_devices()]:
            menu.add_command(label=("✓ " if name == self.audio_device else "   ") + label,
                             command=lambda n=name: self._set_output(n))
        try:
            menu.tk_popup(self.out_lbl.winfo_rootx(), self.out_lbl.winfo_rooty() + self.out_lbl.winfo_height())
        finally:
            menu.grab_release()

    def _set_output(self, name):
        self.audio_device = name
        self._paint_output()
        if self.listener:
            self.listener.device, self.listener.reopen = name, True
        self._save()

    def _delay(self):
        try:
            return max(0, int(self.delay.get()))
        except ValueError:
            return 500

    def _paint_buffer_hint(self):
        b = self._buffer()
        short = b < 250
        self.buffer_hint.config(text="Under 250 ms the TV's Wi-Fi pauses (about 1/4 s) can be heard as skips." if short else "")
        if short:                                           # only takes up room while it has something to say
            self.buffer_hint.pack(fill="x", pady=(6, 0))
        else:
            self.buffer_hint.pack_forget()

    def _on_sound_delay(self, value):
        """The Sound delay slider moved: later sound for a bigger number, applied at once and remembered."""
        ms = int(round(value / 10.0) * 10)
        self.buffer.set(str(ms))
        self.sound_label.config(text=f"{ms} ms")
        self._paint_buffer_hint()
        if self.listener:
            self.listener.set_buffer(ms)
        if getattr(self, "_sound_save", None):
            self.after_cancel(self._sound_save)
        self._sound_save = self.after(800, self._save)             # not on every pixel of the drag

    def _buffer(self):
        try:
            return max(0, min(SOUND_DELAY_MAX, int(self.buffer.get())))
        except ValueError:
            return BUFFER_MS

    def _after_safe(self, fn):
        try:
            self.after(0, fn)          # from a worker thread: hop to the Tk thread
        except RuntimeError:           # window already closed
            pass

    def _on_volume(self, value):
        """The slider moved (not called when the code moves it): drive whichever volume it currently stands for."""
        if self.listener:
            self.pc_volume = int(value)
            self.listener.volume = self.pc_volume / 100
        else:
            self.tvvol.set_target(int(value))
        self._refresh_volume()

    def _tv_volume_changed(self):
        """The TV announced a volume change (from this app or from the remote): follow it."""
        self._refresh_volume()

    def _refresh_volume(self):
        """Make the control show the right thing for where the sound is going right now."""
        if self.listener:                                   # routed to this PC: the PC's own gain, 0-200%
            if self.scale.hi != 200:
                self.scale.set_range(0, 200, self.pc_volume, mark=100)
            self.vol_title.config(text="Volume  ·  this PC")
            self.vol_label.config(text=f"{self.pc_volume}%")
            self.mute_lbl.pack_forget()
            return
        tv = self.tvvol                                     # playing on the Roku: the TV's own volume, 0-100
        if self.scale.hi != 100:
            self.scale.set_range(0, 100, tv.level or 0, mark=None)
        elif tv.level is not None and not self.scale.dragging and tv.target is None:
            self.scale.set_range(0, 100, tv.level, mark=None)
        self.vol_title.config(text="Volume  ·  TV")
        self.vol_label.config(text="—" if tv.level is None else str(tv.level))
        if not self.mute_lbl.winfo_ismapped():
            self.mute_lbl.pack(side="right", padx=(0, 10))
        self.mute_lbl.config(text="Muted" if tv.muted else "Mute", fg=RED if tv.muted else MUTED)

    def _set_dot(self, text):
        color = GREEN if text.startswith("Connected") else AMBER if text.startswith("Connecting") \
            else GREY if text in ("Not connected", "Disconnected") else RED
        self.dot.set(color)

    def set_status(self, text, ended=False):
        # called from worker threads: hop back to the Tk thread
        log(f"status: {text}{' (ended)' if ended else ''}")
        self.after(0, self._apply_status, text, ended)

    def _apply_status(self, text, ended):
        self.status.set(text)
        self._set_dot(text)
        if ended and self.listener:
            self.disconnect(keep_status=True)

    # ---- actions ---------------------------------------------------------
    def toggle(self):
        if self.listener:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        listener = Listener(self.host.get().strip() or DEFAULT_HOST, self._delay(), self.set_status, self._buffer())
        listener.volume = self.pc_volume / 100
        listener.device = self.audio_device
        self.btn.style("Connecting…", ACCENT, ACCENT_HI, enabled=False)
        self.update_idletasks()
        try:
            listener.start()
        except Exception as e:
            listener.stop()
            self.status.set(str(e))
            self._set_dot(str(e))
            self.btn.style("Connect", ACCENT, ACCENT_HI)
            return
        self.listener = listener
        self._save()
        self._set_dot("Connecting")
        for w in (self.host_entry, self.delay_entry):
            w.config(state="disabled")
        self.btn.style("Disconnect", "#26262b", "#34343a")
        self._refresh_volume()                              # the sound now goes to this PC: slider = PC volume

    def disconnect(self, keep_status=False):
        if self.listener:
            threading.Thread(target=self.listener.stop, daemon=True).start()
            self.listener = None
        if not keep_status:
            self.status.set("Not connected")
            self._set_dot("Not connected")
        for w in (self.host_entry, self.delay_entry):
            w.config(state="normal")
        self.btn.style("Connect", ACCENT, ACCENT_HI)
        self._refresh_volume()                              # the sound is back on the TV: slider = TV volume

    def close(self):
        self._save()
        self.phone.stop()
        self.kb_watch.stop()
        self.chrome.release()
        self.web.close()
        self.discord.stop()
        self.bridge.stop()
        if self.listener:
            self.listener.stop()
        self.destroy()


def already_running():
    """True (and the existing window is brought forward) if another CapoCast is already open."""
    try:
        kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        globals()["_instance_mutex"] = kernel32.CreateMutexW(None, False, "Local\\RokuListen.SingleInstance")
        if kernel32.GetLastError() != 183:   # ERROR_ALREADY_EXISTS
            return False
        hwnd = user32.FindWindowW(None, "CapoCast")
        if hwnd:
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    try:
        ctypes.windll.user32.SetProcessDPIAware()   # real pixel coords, so the embedded Chrome lines up exactly
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RokuListen")   # own taskbar icon
    except Exception:
        pass
    App().mainloop()
