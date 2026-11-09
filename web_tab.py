"""The Web tab: any site in a protected, headless Brave, with a Cast to TV button.

Flow: you browse and press play. web_guard (talking to Brave over DevTools) blocks pop-ups, redirects and click
traps, records the streams the page loads, and streams the page's picture to web_view, which draws it here. Cast to
TV picks the real stream (stream_finder), serves it to the Roku through media_relay (which replays the site's
headers) and starts it in the RokuCast channel on the TV.

Brave has no window of its own, so nothing ever opens on the desktop or takes focus from a game.

The UI widgets and colors are handed in (`ui`) instead of imported: this file is loaded by roku_listen_gui.py, and
importing that file back would load it a second time.
"""
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog
from urllib.parse import urlparse

import brave_profile
import discord_presence
import drm
import subtitles
import upscaler
from media_relay import MediaRelay, probe_all
from next_episode import NextEpisode, next_url
from roku_listen import local_ip_for
from stream_finder import MIN_SECONDS, Candidate, apply_page_videos, choose, score
from web_guard import WebGuard, normalize_address
from web_sync import WebSync
from web_view import PageView, key_events

HOME = "https://duckduckgo.com"
UPSCALE_NAMES = {"off": "Off", "fast": "Anime4K", "hq": "Anime4K HQ", "movie": "Movie"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CastError(Exception):
    """A problem to tell the user about, in plain words."""


class Cancelled(Exception):
    """You pressed Cancel: this cast's worker stops where it is."""


def ytdlp_candidate(page_url):
    """Last resort when the browser saw no stream: ask yt-dlp (if installed) what the page's video is."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-J", "--no-playlist", "--no-warnings", "-f", "best[protocol^=m3u8]/best[ext=mp4]/best",
                              page_url], capture_output=True, text=True, timeout=45, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise CastError("yt-dlp took too long looking at that page.")
    if out.returncode != 0 or not out.stdout.strip():
        return None
    info = json.loads(out.stdout)
    if info.get("_type") == "playlist":
        info = next((e for e in info.get("entries") or [] if e), {})
    if info.get("has_drm"):
        raise CastError("That video is DRM-protected, which CapoCast won't cast.")
    url = info.get("url")
    if not url:
        return None
    kind = "hls" if "m3u8" in (info.get("protocol") or "") else "mp4" if info.get("ext") == "mp4" else None
    if not kind:
        raise CastError(f"The video is {info.get('ext') or 'in a format'} that CapoCast can't relay yet (it handles "
                        "HLS, DASH and MP4).")
    return Candidate(url=url, kind=kind, headers=info.get("http_headers") or {},
                     duration=float(info.get("duration") or 0), height=int(info.get("height") or 0),
                     width=int(info.get("width") or 0), live=bool(info.get("is_live")))


class Tv:
    """What WebSync needs from the Roku, bound to whatever IP is in the box right now."""

    def __init__(self, remote, host_fn):
        self.remote, self.host_fn = remote, host_fn

    def state(self):
        return self.remote.state(self.host_fn())

    def key(self, name):
        self.remote.key(self.host_fn(), name)

    def cast(self, url):
        self.remote.cast_url(self.host_fn(), url)


class WebTab:
    discord, cast_since = None, 0.0                       # defaults for anything built without __init__
    upscale_owner, tv_gone, cast_image, cast_session = None, 0, None, None
    hdr = False                                           # the setting; __init__ gives each tab its own
    cast_end = None                                           # when the video will end by Discord's clock, if its length is known
    subs, sub_dir = "", None                                  # subtitle language chosen ('' = off); folder for fetched files
    cast_site = ""                                           # the site being cast from, for the Discord status
    _reattach_at, _reattach_tries = 0.0, 0                    # reconnecting to the browser after its DevTools link dropped

    SOURCE = "web"                                        # this tab's name in the shared Discord status
    REATTACH_MAX = 60.0                                   # longest wait between reconnect attempts, seconds
    STEADY = 30.0                                         # a link that has held this long resets the reconnect back-off

    def __init__(self, app, page, ui, home=None, upscale=None, discord=None, subs="", hdr=False):
        self.app, self.page, self.ui = app, page, ui
        self.discord = discord                            # discord_presence.Status, shared with the YouTube tab
        self.cast_since = 0.0                             # when the current cast began, for Discord's timer
        self.home = home or HOME
        self.upscale = upscale if upscale in UPSCALE_NAMES else "off"
        self.subs = str(subs or "").strip().lower()
        self.hdr = bool(hdr)                              # lift SDR video to (simulated) HDR on this PC
        self.upscaled = None                              # what the TV's upscaled cast was made with, while there is one
        self.guard = WebGuard(ui.log, home=self.home)
        self.guard.on_youtube = self._youtube_tapped
        self.proc = None
        self.relay = None
        self.sync = None                                  # follows the page's video on the TV after a cast
        self.tv = Tv(ui.Remote, lambda: app._host)
        self.profile = brave_profile.profile_dir()
        self.visible = self.busy = False
        self.attempt, self.cast_at = 0, 0.0               # which cast is current; a worker from an older one stands down
        self.next, self.cast_page = NextEpisode(), ""     # follows the page on to the next episode
        self.cast_next, self.link_at = "", None           # the episode after the cast one (if known); where a resume starts
        self._progress_at = 0.0                           # when Continue Watching was last written
        self.launched_at = 0.0
        self.filters_ok = True
        self.note, self.note_until = ("", ui.MUTED), 0.0
        self._build()
        app.after(250, self._tick)

    @property
    def discord_on(self):
        return bool(self.discord and self.discord.is_on(self.SOURCE))

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ---- layout ------------------------------------------------------------------
    def _build(self):
        ui = self.ui
        bar = tk.Frame(self.page, bg=ui.BG)
        bar.pack(fill="x", padx=16, pady=(10, 4))
        for kind, cmd in (("back", lambda: self.guard.back()), ("fwd", lambda: self.guard.forward()),
                          ("reload", lambda: self.guard.reload())):
            ui.NavButton(bar, kind, cmd).pack(side="left", padx=(0, 2))
        self.url = tk.StringVar()
        self.entry = tk.Entry(bar, textvariable=self.url, bg=ui.CARD, fg=ui.FG, insertbackground=ui.FG, relief="flat",
                              font=(ui.FONT, 11), highlightthickness=1, highlightbackground=ui.EDGE,
                              highlightcolor=ui.ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 12), ipady=7, ipadx=8)
        self.entry.bind("<Return>", self._go)
        self.entry.bind("<FocusIn>", lambda e: self.entry.after(1, lambda: self.entry.select_range(0, "end")))
        self.cast_btn = ui.RoundButton(bar, "Cast to TV", self.cast, width=130, height=38)
        self.cast_btn.pack(side="left")

        strip = tk.Frame(self.page, bg=ui.BG)
        strip.pack(fill="x", padx=18, pady=(0, 6))
        self.status = tk.Label(strip, text="", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 9), anchor="w")
        self.status.pack(side="left")
        self.allow = tk.Label(strip, text="", bg=ui.BG, fg=ui.ACCENT, font=(ui.FONT, 9, "bold"), cursor="hand2")
        self.allow.bind("<Button-1>", lambda e: self.guard.allow_last_block())
        # Start page: "Home" goes to it, "Set as home" makes the page you're on the one the Web tab opens with.
        # one small pill per option (the accent colour means "on"), right to left
        chip = lambda text, cmd, tip, **kw: self._chip(strip, text, cmd, tip, **kw)
        self.sethome_lbl = chip("Set as home", self.set_home, "Open this page every time the Web tab starts")
        self.home_lbl = chip("Home", self.go_home, "Go to your home page")
        self.continue_lbl = chip("Continue", self.show_continue, "Pick up a show where you left off")
        self.upscale_lbl = chip("", self.next_upscale, "Sharpen the picture to 4K on this PC before it goes to the TV")
        self._paint_upscale()
        self.hdr_lbl = chip("", self.toggle_hdr, "Lift the highlights to HDR on this PC before it goes to the TV")
        self._paint_hdr()
        self.subs_lbl = chip("", self.next_subs, "Draw subtitles onto the video on this PC")
        self._paint_subs()
        self.discord_lbl = chip("", self.toggle_discord, "Show what you're watching on Discord")
        self._paint_discord()
        self.stop_lbl = ui.Chip(strip, "Stop casting", self.stop_casting, solid=True)    # shown only while casting

        self.host = tk.Frame(self.page, bg=ui.BG)
        self.host.pack(fill="both", expand=True)
        self.view = PageView(self.host, lambda: self.guard, ui.BG, self._resized)
        self.view.pack(fill="both", expand=True)
        self.msg = tk.Label(self.host, text="", bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 11), justify="center")
        self.msg.place(relx=0.5, rely=0.45, anchor="center")
        self.msg.bind("<Button-1>", lambda e: self.launch())

    def _chip(self, strip, text, command, tip, **kw):
        c = self.ui.Chip(strip, text, command, **kw)
        c.pack(side="right", padx=(0, 8))
        self.ui.Tooltip(c, tip)
        return c

    def _message(self, text, clickable=False):
        self.msg.config(text=text, fg=self.ui.FG if clickable else self.ui.MUTED, cursor="hand2" if clickable else "")

    def _note(self, text, color=None, seconds=8):
        self.note, self.note_until = (text, color or self.ui.MUTED), time.time() + seconds

    def _resized(self, w, h):
        self.guard.set_view(w, h, self.visible)

    # ---- tab lifecycle ---------------------------------------------------------------
    def on_show(self):
        self.visible = True
        w, h = self.view.winfo_width(), self.view.winfo_height()
        if w > 50 and h > 50:
            self.guard.set_view(w, h, True)
        if not self.alive():
            self.launch()

    def on_hide(self):
        self.visible = False
        w, h = self.view.winfo_width(), self.view.winfo_height()
        if w > 50 and h > 50 and self.guard._view:
            self.guard.set_view(w, h, False)             # stop streaming frames nobody is looking at

    def launch(self):
        if self.alive():
            return
        exe = brave_profile.find_brave()
        if not exe:
            self._message("The Web tab needs the Brave browser (it blocks the ads).\n\n"
                          "Install it with:  winget install Brave.Brave\nthen click here.", clickable=True)
            return
        self._message("Opening the Web tab…")
        try:
            self.filters_ok = brave_profile.seed_filters(self.profile, self.ui.log)
            try:
                os.remove(brave_profile.port_file(self.profile))
            except OSError:
                pass
            if self.sync and self.sync.active:
                self.sync.stop(WebSync.BROWSER_GONE)
            self.guard.stop()
            self.guard = WebGuard(self.ui.log, home=self.home)         # fresh state for the new browser
            self.guard.on_youtube = self._youtube_tapped
            self.view.reset()
            w, h = self.view.winfo_width(), self.view.winfo_height()
            self.guard.set_view(w if w > 50 else 1280, h if h > 50 else 700, self.visible)
            self.proc = subprocess.Popen([exe, *brave_profile.launch_args(self.profile, self.home)],
                                         creationflags=NO_WINDOW)
            self.launched_at = time.time()
        except Exception as e:
            self.ui.log(f"web launch failed: {e!r}")
            self._message(f"{e}  ·  click to retry", clickable=True)

    def _reattach(self):
        """Reconnect to the still-running browser after the DevTools link dropped, instead of leaving the tab frozen with
        protection off until the app is restarted (what happened after the PC slept on 2026-11-08). A fresh guard is
        built because the old one's sessions died with the link; the page, its streams and a cast in progress carry on."""
        old = self.guard
        self._reattach_tries += 1
        self._reattach_at = time.time() + min(self.REATTACH_MAX, 2.0 * 2 ** min(self._reattach_tries, 5))
        pf = brave_profile.port_file(self.profile)
        if not os.path.exists(pf):
            return
        new = WebGuard(self.ui.log, home=self.home)
        new.on_youtube = self._youtube_tapped
        if old._view:
            new.set_view(*old._view)
        with old._lock:                               # what the old guard knew about this page is still true
            new._cands, new._url, new._title, new._page_key = dict(old._cands), old._url, old._title, old._page_key
        try:
            new.attach(pf)
        except Exception as e:
            self.ui.log(f"web guard reconnect failed (try {self._reattach_tries}): {e!r}")
            return
        old.stop()
        self.guard = new
        if self.sync is not None:
            self.sync.guard = new
            self.sync.epoch0 = new.epoch              # the same page seen by a new guard, not "you left the page"
        self.ui.log(f"web guard reconnected to the browser (try {self._reattach_tries})")

    def on_resume(self):
        """The PC just woke up: check the browser link now rather than at the next back-off step."""
        self._reattach_at = 0.0

    def _tick(self):
        try:
            if self.proc is not None and self.proc.poll() is not None:
                self.proc = None
                if self.sync and self.sync.active:
                    self.sync.stop(WebSync.BROWSER_GONE)       # nothing can report the page's video any more
                self.guard.stop()
                self._message("The browser stopped  ·  click to reopen", clickable=True)
            if self.alive():
                if not self.guard.running and not self.guard.error:
                    pf = brave_profile.port_file(self.profile)
                    if os.path.exists(pf):
                        try:
                            self.guard.attach(pf)
                        except Exception as e:
                            self.ui.log(f"web guard attach failed: {e!r}")
                            self.guard.error = "Protection couldn't start"
                    elif time.time() - self.launched_at > 40:
                        self._message("Couldn't open the Web tab  ·  click to retry", clickable=True)
                        self.proc.terminate()
                elif not self.guard.running and time.time() >= self._reattach_at:
                    self._reattach()       # the link dropped (the PC slept, say) but Brave is still up: reconnect
                elif self.guard.running and time.time() - self.guard.attached_at > self.STEADY:
                    self._reattach_tries = 0
                if self.view.has_frame and self.msg.cget("text"):
                    self._message("")
                self._watch_next()
                self.guard.phone_expire()
            self._refresh()
        except Exception as e:
            self.ui.log(f"web tab error: {e!r}")
        self.app.after(250, self._tick)

    # ---- status line ---------------------------------------------------------------------
    def _refresh(self):
        ui = self.ui
        snap = self.guard.snapshot()
        self._record_visit(snap)
        if snap["url"] and self.app._focused() is not self.entry and self.url.get() != snap["url"]:
            self.url.set(snap["url"])
        if time.time() < self.note_until:
            text, color = self.note
        elif self.guard.phone_active:
            text, color = "Your phone is browsing here", ui.GREEN
        elif not self.alive():
            text, color = "", ui.MUTED
        elif snap["error"] or self.guard.error:
            text, color = (snap["error"] or self.guard.error) + " - pop-up and redirect protection is off", ui.RED
        elif not snap["running"]:
            text, color = "Starting protection…", ui.AMBER
        else:
            s, parts = snap["stats"], []
            if s["popups"]:
                parts.append(f"{s['popups']} pop-up{'s' * (s['popups'] > 1)} blocked")
            if s["redirects"]:
                parts.append(f"{s['redirects']} redirect{'s' * (s['redirects'] > 1)} stopped")
            if s["overlays"]:
                parts.append(f"{s['overlays']} click trap{'s' * (s['overlays'] > 1)} removed")
            if s["ads"]:
                parts.append(f"{s['ads']} ad requests blocked")
            if snap["streams"]:
                parts.append(f"{snap['streams']} video stream{'s' * (snap['streams'] > 1)} found")
            text = "  ·  ".join(parts) or "Protection on"
            color = ui.GREEN
            if not self.filters_ok:
                text = "Ad filters missing: open Brave once so it can download them  ·  " + text
                color = ui.AMBER
        self.status.config(text=text, fg=color)
        lb = snap["last_block"]
        if lb and lb["what"] in ("redirect", "pop-up") and lb.get("url") and time.time() >= self.note_until:
            self.allow.config(text=f"   Blocked {urlparse(lb['url']).hostname or lb['url']}  ·  Allow")
            if not self.allow.winfo_ismapped():
                self.allow.pack(side="left")
        else:
            self.allow.pack_forget()
        following = bool(self.sync and self.sync.active)
        if following:
            if not self.stop_lbl.winfo_ismapped():
                self.stop_lbl.pack(side="right")
        else:
            self.stop_lbl.pack_forget()
        self._paint_cast_button()

    def _paint_cast_button(self):
        if self.busy or self.next.counting:
            self.cast_btn.style("Cancel", "#26262b", "#34343a")
        else:
            self.cast_btn.style("Cast to TV", self.ui.ACCENT, self.ui.ACCENT_HI)

    def _paint_upscale(self):
        on = self.upscale != "off"
        self.upscale_lbl.config(text="Upscale: " + UPSCALE_NAMES[self.upscale], fg=self.ui.ACCENT if on else self.ui.MUTED)

    def _paint_hdr(self):
        self.hdr_lbl.config(text="HDR: " + ("On" if self.hdr else "Off"), fg=self.ui.ACCENT if self.hdr else self.ui.MUTED)

    def toggle_hdr(self):
        """Clicking the label turns the simulated HDR on or off. It applies from the next cast."""
        self.hdr = not self.hdr
        why = upscaler.missing(upscaler.BURN, False, True) if self.hdr else ""
        if why:
            self.hdr = False
            self._note(why, self.ui.AMBER, 12)
        self._paint_hdr()
        self._save_settings()
        if self.hdr:
            self._note("Next cast: simulated HDR, made on this PC's GPU (the TV switches to its HDR mode)",
                       self.ui.MUTED, 6)

    def _paint_subs(self):
        self.subs_lbl.config(text="Subtitles: " + (self.subs.title() or "Off"),
                             fg=self.ui.ACCENT if self.subs else self.ui.MUTED)

    def next_subs(self):
        """Clicking the label steps through Off and each subtitle language the page has offered so far. It applies from
        the next cast (the PC draws the words onto the video, since the TV can't show a separate subtitle file), and the
        language is remembered for other pages."""
        tracks = self._page_tracks()
        options = [""]
        for t in tracks:
            want = (t["lang"] or t["label"]).lower()
            if want not in options:
                options.append(want)
        if len(options) == 1:
            if self.subs:
                self.subs = ""
                self._save_settings()
            else:
                self._note("No subtitle files seen on this page yet. Turn captions on in the video's player, then "
                           "click again.", self.ui.AMBER, 10)
        else:
            current = options.index(self.subs) if self.subs in options else 0
            self.subs = options[(current + 1) % len(options)]
            why = upscaler.missing(upscaler.BURN, True) if self.subs else ""
            if why:
                self.subs = ""
                self._note(why, self.ui.AMBER, 12)
            self._save_settings()
        self._paint_subs()
        if self.subs:
            self._note(f"Next cast: {self.subs.title()} subtitles, drawn onto the video on this PC", self.ui.MUTED, 6)

    def _save_settings(self):
        save = getattr(self.app, "_save", None)
        if save:
            save()

    def _page_tracks(self, cand=None):
        """The subtitle tracks this page offers: the files its player fetched, its <track> elements, and (given the
        stream being cast) the ones its master playlist lists."""
        files, dom = self.guard.subtitle_sources(cand.duration if cand else 0)
        renditions = subtitles.fetch_renditions(cand.url, cand.headers) if cand else []
        return subtitles.build_tracks(files, dom, renditions, cand.headers if cand else None)

    def _prepare_subs(self, attempt, cand):
        """Worker thread: fetch the chosen subtitle track to this PC as a styled ASS file. Returns its path, or '' to
        cast without (saying why)."""
        if not self.subs:
            return ""
        ui = self.ui
        tracks = self._page_tracks(cand)
        track = subtitles.pick_track(tracks, self.subs)
        if track is None:
            hint = "" if tracks else " (the player hasn't loaded any: turn its captions on first)"
            self._post(lambda: self._note(f"No {self.subs.title()} subtitles on this page{hint}, casting without",
                                          ui.AMBER, 12))
            return ""
        self._post(lambda: self._note(f"Getting the {track['label']} subtitles…", ui.AMBER, 60))
        if self.sub_dir is None:
            self.sub_dir = tempfile.mkdtemp(prefix="capocast-subs-")
        out = os.path.join(self.sub_dir, f"subs{attempt}.ass")
        ok = subtitles.to_ass(track, out, ui.log)
        self._check(attempt)
        if not ok:
            self._post(lambda: self._note("Couldn't get those subtitles, casting without", ui.AMBER, 10))
            return ""
        ui.log(f"subtitles: {track['label']} from {track['url'][:110]}")
        for name in os.listdir(self.sub_dir):                  # earlier casts' files aren't needed any more
            if name != os.path.basename(out):
                try:
                    os.remove(os.path.join(self.sub_dir, name))
                except OSError:
                    pass
        return out

    def _paint_discord(self):
        self.discord_lbl.config(text="Discord: " + ("On" if self.discord_on else "Off"),
                                fg=self.ui.ACCENT if self.discord_on else self.ui.MUTED)

    def toggle_discord(self):
        """Show what's on the TV as your Discord status. Off until you turn it on, and asks for the application ID
        the first time: Discord only shows a status for an application you've registered."""
        if self.discord is None:
            return
        if self.discord.enabled.get(self.SOURCE):
            self.discord.enable(self.SOURCE, False)
        else:
            if not self.discord.client_id:
                found, problem = discord_presence.ask_application_id(self.app)
                if problem:
                    return self._note(problem, self.ui.AMBER, 10)
                if not found:
                    return
                self.discord.set_id(found)
            self.discord.enable(self.SOURCE, True)
            if not discord_presence.discord_running():
                self._note("Discord isn't open. The status will appear once it is.", self.ui.AMBER, 8)
            if self.sync is not None and self.sync.active:       # already casting: show it now, Discord open or not
                self._set_presence(self.sync.title or "Watching", self.cast_image, self.cast_site)
        self._paint_discord()
        save = getattr(self.app, "_save", None)
        if save:
            save()

    def _set_presence(self, title, image=None, site=""):
        """Show `title` as the Discord status, laid out like the YouTube one: the name of what's playing, the site it's
        on (and the upscaling, if any) underneath, and its thumbnail beside them. None takes the status down. Does
        nothing when it's off."""
        if self.discord is None:
            return
        if title is None:
            self.discord.clear(self.SOURCE)
            return
        plan = self.upscaled or {}
        mode = plan.get("mode")
        state = f"on {site}" if site else "Casting to the TV"
        if mode in UPSCALE_NAMES:
            name = UPSCALE_NAMES[mode]
            state += f" · {name} upscale" if "4K" in name else f" · 4K upscale ({name})"      # never says 4K twice
        if plan.get("subs"):
            state += " · subtitles"
        if plan.get("hdr"):
            state += " · HDR"
        extra = {"end": self.cast_end} if self.cast_end else {}
        self.discord.show(self.SOURCE, f"Watching {title}", state, self.cast_since or None, image,
                          image_text=f"{title} · {site}" if site else title, **extra)

    def next_upscale(self):
        """Clicking the label steps through Off, Anime4K, Anime4K HQ and Movie. It applies from the next cast."""
        names = list(UPSCALE_NAMES)
        self.upscale = names[(names.index(self.upscale) + 1) % len(names)]
        if self.upscale != "off":
            why = upscaler.missing()
            if why:
                self.upscale = "off"
                self._note(why, self.ui.AMBER, 12)
        self._paint_upscale()
        save = getattr(self.app, "_save", None)
        if save:
            save()
        if self.upscale != "off":
            self._note(f"Next cast: upscaled to 4K ({UPSCALE_NAMES[self.upscale]}) on this PC's GPU", self.ui.MUTED, 6)

    def stop_casting(self):
        """Stop following the page, give the page its sound back, and leave the RokuCast channel on the TV."""
        self.next.disarm()
        if self.sync:
            self.sync.stop("Stopped")
        threading.Thread(target=self._tv_back, daemon=True).start()

    def _tv_back(self):
        try:
            self.tv.key("Back")
        except Exception as e:
            self.ui.log(f"stop casting: {e!r}")

    def _following_stopped(self, reason):
        """Called from WebSync's thread when it gives up (page left, TV stopped, or you pressed Stop)."""
        sync = self.sync
        if sync is not None and not sync.live and sync.dur >= MIN_SECONDS and self.cast_page and reason:
            seen = float(getattr(sync, "tv_seen", 0) or 0)
            if reason == WebSync.FINISHED:
                self._save_progress(self.cast_page, sync.dur, sync.dur, sync.title, finished=True)
            elif seen > 0:
                self._save_progress(self.cast_page, seen, sync.dur, sync.title)
        self._post(self.next.grace if reason in (WebSync.LEFT_PAGE, WebSync.FINISHED) else self.next.disarm)
        if reason in ("Stopped", WebSync.FINISHED, WebSync.TV_STOPPED):
            self._set_presence(None)                      # leaving the page doesn't count: the TV is still playing it
        if self.relay and reason in ("Stopped", WebSync.FINISHED, WebSync.TV_STOPPED):
            self.upscaled = None
            self.relay.stop_upscaling()                   # leaving the page doesn't count: the TV is still playing it
        if reason and reason != "Stopped":
            self._post(lambda: self._note(reason, self.ui.AMBER, 12))
        elif reason == "Stopped":
            self._post(lambda: self._note("Stopped casting", self.ui.MUTED, 6))

    def _feed_clock(self, st):
        """Give Discord the TV's real position and play/pause state, so its clock follows what's being watched (pausing,
        scrubbing, buffering) rather than the time since Cast was pressed. Position in the video is the TV's position plus
        where the TV's stream began."""
        sync = self.sync
        if not (self.discord and st and sync is not None and sync.active and not sync.live):
            return
        if self.discord.showing != self.SOURCE or time.time() < sync.settle_until:
            return                                        # a restart in progress still reports the old position
        if st["state"] not in ("play", "pause") or st.get("app") not in ("RokuCast", ""):
            return                                        # buffering or elsewhere: leave the clock as it is
        pos = st["pos"] + sync.offset
        dur = sync.dur or ((st["dur"] + sync.offset) if st["dur"] else 0)
        self.discord.progress(self.SOURCE, pos, dur, st["state"] == "play")

    PROGRESS_EVERY = 15.0        # seconds between writes of where the TV is, for Continue Watching

    def _record_progress(self, st):
        """Remember where the TV is in what's being followed, so it can be picked up again later."""
        store, sync = getattr(self.app, "phone_store", None), self.sync
        if not (store and st and sync is not None and sync.active and not sync.live and self.cast_page):
            return
        if time.time() < sync.settle_until or st["state"] not in ("play", "pause") or st.get("app") not in ("RokuCast", ""):
            return
        dur = sync.dur or ((st["dur"] + sync.offset) if st["dur"] else 0)
        if dur < MIN_SECONDS or time.time() - self._progress_at < self.PROGRESS_EVERY:
            return
        self._progress_at = time.time()
        self._save_progress(self.cast_page, st["pos"] + sync.offset, dur, sync.title)

    def _save_progress(self, page, pos, dur, title, finished=False):
        """Write one Continue Watching row, off the calling thread (the disk may be busy)."""
        store = getattr(self.app, "phone_store", None)
        if not store:
            return
        site, image, nxt = self.cast_site, self.cast_image, self.cast_next

        def run():
            try:
                store.save_progress(page, pos, dur, title, site, image, nxt, finished)
            except Exception as e:
                self.ui.log(f"continue watching: {e!r}")
        threading.Thread(target=run, name="continue-save", daemon=True).start()

    def tv_state(self, st):
        """Fed by the app's once-a-second look at the TV (None if it can't be reached). WebSync notices the TV leaving
        while the page is being followed; this covers the rest: you left the page, or a recast failed, and the TV has
        since gone somewhere else. Then the Discord status comes down and the encoder stops, rather than both carrying
        on for a cast nobody is watching."""
        self._feed_clock(st)
        self._record_progress(st)
        left_over = bool(self.upscaled or (self.discord and self.discord.showing == self.SOURCE))
        following = self.sync is not None and self.sync.active
        if st is None or following or self.busy or not left_over:
            self.tv_gone = 0
            return
        on_cast = st["state"] != "none" and st.get("app") in ("RokuCast", "")
        self.tv_gone = 0 if on_cast else self.tv_gone + 1
        if self.tv_gone < 2:                              # one odd reading while the TV switches isn't enough
            return
        self.tv_gone, self.upscaled = 0, None
        self._set_presence(None)
        if self.relay:
            threading.Thread(target=self.relay.stop_upscaling, daemon=True).start()

    def show_continue(self, event=None):
        """The Continue menu: resume a show where the TV stopped, or play the episode after one that finished."""
        store = getattr(self.app, "phone_store", None)
        ui = self.ui
        menu = tk.Menu(self.app, tearoff=0, bg=ui.CARD, fg=ui.FG, activebackground=ui.ACCENT, activeforeground="#ffffff",
                       bd=0, disabledforeground=ui.MUTED)
        rows = store.continue_list() if store else []
        here = store.progress_for(self.guard.snapshot()["url"]) if store else None

        def play(url, at=None):
            try:
                self._note(self.cast_link(url, at), ui.AMBER, 10)
            except ValueError as e:
                self._note(str(e), ui.AMBER, 10)
        name = lambda r: (r["title"] or urlparse(r["url"]).hostname or r["url"])[:60]
        if here and not here["finished"] and here["pos"] > 10:
            menu.add_command(label=f"Resume this page at {self._clock(here['pos'])}",
                             command=lambda r=here: play(r["url"], r["pos"]))
            menu.add_separator()
        for r in rows:
            if r["finished"]:
                menu.add_command(label=f"Up next after {name(r)}", command=lambda r=r: play(r["next_url"]))
            else:
                menu.add_command(label=f"{name(r)}  ·  {self._clock(max(0, r['dur'] - r['pos']))} left",
                                 command=lambda r=r: play(r["url"], r["pos"]))
        if not rows and not here:
            menu.add_command(label="Nothing to continue yet", state="disabled")
        else:
            menu.add_separator()
            menu.add_command(label="Clear list", command=store.clear_watching)
        try:
            menu.tk_popup(self.continue_lbl.winfo_rootx(), self.continue_lbl.winfo_rooty() + self.continue_lbl.winfo_height())
        finally:
            menu.grab_release()

    def go_home(self):
        self.guard.navigate(self.home)

    def set_home(self):
        """Make the page you're on the Web tab's start page (saved, and used every time the tab opens)."""
        url = self.guard.snapshot()["url"]
        if not url.startswith(("http://", "https://")):
            return self._note("Open a page first, then set it as your home page.", self.ui.AMBER)
        self.home = self.guard.home = url
        save = getattr(self.app, "_save", None)
        if save:
            save()
        self._note("Home page set to " + (urlparse(url).hostname or url), self.ui.GREEN, 6)

    def _go(self, _e=None):
        self.guard.navigate(normalize_address(self.url.get(), self.home))
        self.view.focus_set()

    # ---- casting -------------------------------------------------------------------------------
    def _watch_next(self):
        """The page has moved on to the next episode: warn, then take the TV there too."""
        if self.busy:
            return
        snap = self.guard.snapshot()
        left = self.next.tick(snap["url"], self.guard.page_videos(), self.guard.has_fresh_stream())
        if left is None:
            return
        if left > 0:
            title = snap["title"] if len(snap["title"]) <= 52 else snap["title"][:51] + "…"
            return self._note(f"Up next{': ' + title if title else ''}  ·  on the TV in {math.ceil(left)}s",
                              self.ui.GREEN, 1)
        self.ui.log(f"next episode: following the page to {snap['url'][:100]}")
        self.next.disarm()
        self.cast()

    def cast(self):
        if self.busy:
            return self.cancel_cast()
        snap = self.guard.snapshot()
        if self.next.counting:
            self.next.skip(snap["url"])
            return self._note("Staying on what's playing", self.ui.MUTED, 6)
        if not self.alive() or not snap["running"]:
            return self._note("Wait for the page to finish opening, then try again.", self.ui.AMBER)
        refusal = drm.refusal_for(snap["url"])
        if refusal:
            return messagebox.showinfo("Can't cast this", refusal, parent=self.app)
        self.busy, self.cast_at, self.cast_page = True, time.time(), snap["url"]
        self.attempt += 1
        self.next.disarm()
        if self.sync:
            self.sync.stop("", quiet=True)                # a new cast replaces the old one
        self._note("Looking for the video…", self.ui.AMBER, 60)
        threading.Thread(target=self._guarded, args=(self.attempt, self._find, snap["url"], snap["title"]),
                         daemon=True).start()

    # ---- casting a link sent from the phone ---------------------------------------------------------------
    LINK_OPEN_WAIT, LINK_STREAM_WAIT, LINK_SETTLE = 45.0, 25.0, 3.0   # seconds: browser ready / video seen / let it start

    link = None                                           # (url, started, navigated, stream_seen_at) while one is being set up

    RESUME_SETTLE = 2.5                                   # seconds for the page's player to move before casting from there

    def cast_link(self, url, at=None):
        """From any thread: open `url` in this tab's browser and cast it once a video stream shows up, starting `at`
        seconds in if given. Returns the message to show on the phone, or raises ValueError with the reason it can't be
        done."""
        url = normalize_address(url, self.home)           # plain words become a search, never a script: always http(s)
        refusal = drm.refusal_for(url)
        if refusal:
            raise ValueError(refusal.splitlines()[0])
        if self.busy:
            raise ValueError("A cast is already starting on the PC. Wait a moment, or stop it first.")
        self._post(lambda: self._link_begin(url, at))
        return "Opening it on the PC, then it goes to the TV" if not at else \
            f"Opening it on the PC, then it resumes at {self._clock(at)} on the TV"

    @staticmethod
    def _clock(seconds):
        s = int(max(0, seconds))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _link_begin(self, url, at=None):
        if self.busy:
            return
        self.next.disarm()
        self.link_at = at
        self.link = (url, time.time(), False, 0.0)
        self._note("From your phone: opening the page…", self.ui.AMBER, 60)
        if not self.alive():
            self.launch()
        self.app.after(500, self._link_step)

    def _link_step(self):
        link = self.link
        if not link:
            return
        url, t0, navigated, seen = link
        now = time.time()
        if self.busy:                                     # someone pressed Cast on the PC in the meantime
            self.link = None
            return
        if not navigated:
            if self.alive() and self.guard.snapshot()["running"]:
                self.guard.navigate(url)
                self.link = (url, now, True, 0.0)
            elif now - t0 > self.LINK_OPEN_WAIT:
                self.link = None
                return self._note("From your phone: the Web tab's browser didn't start", self.ui.RED, 12)
        else:
            if not seen and self.guard.has_fresh_stream():
                seen = now
                self.link = (url, t0, True, seen)
                self._note("From your phone: found the video, starting the cast…", self.ui.AMBER, 60)
            if (seen and now - seen >= self.LINK_SETTLE) or now - t0 > self.LINK_STREAM_WAIT:
                self.link = None                          # no stream by now: cast() still tries yt-dlp, or says what's missing
                at, self.link_at = self.link_at, None
                if at:                                    # move the page's player first: the cast starts where it is
                    self.guard.seek_page(at, 0)
                    self._note(f"Resuming at {self._clock(at)}…", self.ui.AMBER, 30)
                    return self.app.after(int(self.RESUME_SETTLE * 1000), self.cast)
                return self.cast()
        self.app.after(500, self._link_step)

    # ---- browsing from the phone ----------------------------------------------------------------------------
    # The phone shows this tab's page as a live picture and sends taps, scrolls and typing back, so everything
    # (the ad blocking, pop-up protection, finding the video, casting) happens here, exactly as with the mouse.
    PHONE_KEYS = {"Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab", "Escape": "Escape"}

    def phone_open(self):
        """Make sure the browser is running (from any thread)."""
        if not self.alive() and time.time() - self.launched_at > 5:
            self._post(lambda: None if self.alive() else self.launch())

    def phone_frame(self, after, width, height, timeout=20.0):
        """Wait for a picture newer than `after` at the phone's size -> (seq, jpeg, (w, h)), or None if nothing changed."""
        self.phone_open()
        end = time.time() + timeout
        while time.time() < end:
            g = self.guard
            if g.running:
                g.phone_watch(width, height)
                seq, data = g.frame()
                if data and seq != after and self._frame_fits(g):
                    return seq, data, g.view_size()
            time.sleep(0.03)
        return None

    @staticmethod
    def _frame_fits(g):
        """False for a frame drawn before the browser switched to the phone's size (it would show squashed)."""
        meta = getattr(g, "frame_meta", None) or {}
        w, h = g.view_size()
        return not meta.get("deviceWidth") or (abs(meta["deviceWidth"] - w) <= 1 and abs(meta.get("deviceHeight", h) - h) <= 1)

    def phone_tap(self, x, y):
        g = self.guard
        g.mouse("mouseMoved", x, y)
        g.mouse("mousePressed", x, y, "left", 1, 1)
        g.mouse("mouseReleased", x, y, "left", 0, 1)

    def phone_scroll(self, x, y, dx, dy):
        self.guard.mouse("mouseWheel", x, y, dx=dx, dy=dy)

    def phone_text(self, text):
        self.guard.text(text)

    def phone_key(self, name):
        keysym = self.PHONE_KEYS.get(name)
        if not keysym:
            raise ValueError("unknown key")
        for kind, ev in key_events(keysym, "", 0):
            self.guard.key(ev)

    def phone_nav(self, action, url=""):
        g = self.guard
        if action == "back":
            g.back()
        elif action == "forward":
            g.forward()
        elif action == "reload":
            g.reload()
        elif action == "home":
            g.navigate(self.home)
        elif action == "go":
            target = normalize_address(url, self.home)
            refusal = drm.refusal_for(target)
            if refusal:
                raise ValueError(refusal.splitlines()[0])
            g.navigate(target)
        else:
            raise ValueError("unknown action")

    YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")

    def phone_youtube(self, on, go=""):
        """The phone's YouTube tab: the real youtube.com in this browser, drawn black, where tapping a video plays it on
        the TV (through Playlet) instead of here. The phone renews `on` every few seconds; `go` opens a page too."""
        if go and (urlparse(go).scheme != "https" or (urlparse(go).hostname or "") not in self.YOUTUBE_HOSTS):
            raise ValueError("That isn't a YouTube address.")
        self.phone_open()
        self.guard.youtube_mode(on, go or None)

    def _youtube_tapped(self, url):
        """A video was tapped on YouTube in the phone's tab (called from the browser thread): play it on the TV."""
        def run():
            try:
                msg = self.app.phone.backend.cast(url)
                self._post(lambda: self._note(msg, self.ui.GREEN, 6))
            except Exception as e:
                self.ui.log(f"youtube tap: {e!r}")
                text = str(e) or "The TV didn't answer."
                self._post(lambda: self._note(text, self.ui.AMBER, 8))
        threading.Thread(target=run, name="youtube-tap", daemon=True).start()

    def phone_info(self):
        snap = self.guard.snapshot()
        note = self.phone_note()
        store = getattr(self.app, "phone_store", None)
        return {"alive": self.alive(), "ready": bool(snap["running"]), "url": snap["url"], "title": snap["title"],
                "bookmarked": bool(store and store.is_bookmarked(snap["url"])),
                "editing": bool(snap.get("editing")),
                "streams": snap["streams"], "busy": self.busy or self.link is not None,
                "following": bool(self.sync and self.sync.active), "watching": self.phone_watching(),
                "note": {"text": note[0], "ok": note[1]} if note else None}

    def phone_watching(self):
        """What the TV is following right now, for the phone: {id, title, url} or None. `id` changes with each new cast,
        so the phone can jump to the page when one starts."""
        sync = self.sync
        if not (sync and sync.active):
            return None
        return {"id": round(sync.started_at, 2), "title": sync.title or "", "url": self.cast_page}

    def phone_transport(self, action, value=0):
        """Play/pause or skip the page's video. While casting it is the page's video that the TV follows, so this is the
        one control that keeps the page, the TV and the Discord clock together."""
        dur = self.sync.dur if self.sync and self.sync.active else 0
        if action == "toggle":
            self.guard.toggle_page(dur)
        elif action == "skip":
            self.guard.skip_page(value, dur)
        else:
            raise ValueError("unknown action")

    _visited = ("", "")

    def _record_visit(self, snap):
        """Feed the phone's history: every page this tab lands on (the title often arrives after the address)."""
        store = getattr(self.app, "phone_store", None)
        key = (snap["url"], snap["title"])
        if store and snap["url"] and key != self._visited:
            self._visited = key
            store.visit(snap["url"], snap["title"])

    def phone_pages(self):
        """Bookmarks, recent pages and the current page's state, for the phone's Pages sheet."""
        store = getattr(self.app, "phone_store", None)
        snap = self.guard.snapshot()
        if not store:
            return {"bookmarks": [], "history": [], "continue": [], "current": {"url": snap["url"], "title": snap["title"], "bookmarked": False}}
        return {"bookmarks": store.all_bookmarks(), "history": store.recent(), "continue": store.continue_list(),
                "current": {"url": snap["url"], "title": snap["title"], "bookmarked": store.is_bookmarked(snap["url"])}}

    def phone_continue(self, action, url=""):
        """The phone's Continue list: resume / start (over) / next on a row, remove it, or clear them all. Returns the
        message to show on the phone."""
        store = getattr(self.app, "phone_store", None)
        if not store:
            raise ValueError("Continue Watching isn't available.")
        if action == "clear":
            store.clear_watching()
            return "Cleared"
        if action == "remove":
            store.remove_watching(url)
            return "Removed"
        row = store.progress_for(url)
        if not row:
            raise ValueError("That one isn't in the list any more.")
        if action == "next":
            if not row["next_url"]:
                raise ValueError("The next episode isn't known for this one.")
            return self.cast_link(row["next_url"])
        if action in ("resume", "start"):
            return self.cast_link(row["url"], row["pos"] if action == "resume" and not row["finished"] else None)
        raise ValueError("unknown action")

    def phone_bookmark(self, action, url=""):
        """toggle: the page being looked at. remove: a saved page by address. Returns whether the current page is saved."""
        store = getattr(self.app, "phone_store", None)
        if not store:
            raise ValueError("Bookmarks aren't available.")
        snap = self.guard.snapshot()
        if action == "toggle":
            return store.toggle_bookmark(snap["url"], snap["title"])
        if action == "remove":
            store.remove_bookmark(url)
            return store.is_bookmarked(snap["url"])
        if action == "clear_history":
            store.clear_history()
            return store.is_bookmarked(snap["url"])
        raise ValueError("unknown action")

    def phone_cast(self):
        """Cast whatever the phone is looking at, like pressing Cast on the PC. Returns what to tell the phone."""
        snap = self.guard.snapshot()
        if not self.alive() or not snap["running"]:
            raise ValueError("The page isn't ready yet.")
        refusal = drm.refusal_for(snap["url"])
        if refusal:
            raise ValueError(refusal.splitlines()[0])
        cancelling = self.busy
        self._post(self.cast)                             # cast() stops a cast in progress when called during one
        return "Cancelled" if cancelling else "Looking for the video…"

    def phone_note(self):
        """What the phone's status line should say about the Web tab, as (text, ok) or None."""
        if time.time() >= self.note_until or not self.note[0]:
            return None
        return self.note[0], self.note[1] == self.ui.GREEN

    def cancel_cast(self):
        self.link = None
        if time.time() - self.cast_at < 0.6:              # the second half of a double-click on Cast
            return
        self.attempt += 1
        self.busy = False
        self._note("Cast cancelled", self.ui.MUTED, 6)

    def _check(self, attempt):
        if attempt != self.attempt:
            raise Cancelled()

    def _post(self, fn):
        try:
            self.app.after(0, fn)
        except RuntimeError:        # window already closed
            pass

    def _guarded(self, attempt, fn, *args):
        try:
            fn(attempt, *args)
        except Cancelled:
            pass
        except CastError as e:
            message = str(e)                              # Python deletes `e` before the UI gets to run the lambda
            self._post(lambda: self._fail(attempt, message))
        except Exception as e:
            self.ui.log(f"cast failed: {e!r}")
            message = f"Something went wrong: {e}"
            self._post(lambda: self._fail(attempt, message))

    def _fail(self, attempt, message):
        if attempt != self.attempt:
            return
        self.ui.log(f"cast failed: {message.splitlines()[0]}")
        self.busy = False
        self._note(message.splitlines()[0], self.ui.RED, 12)
        if len(message) > 90 or "\n" in message:
            messagebox.showinfo("Can't cast this", message, parent=self.app)

    def _find(self, attempt, page_url, title):
        """Worker thread: decide which stream is the video."""
        cands = self.guard.candidates()
        apply_page_videos(cands, self.guard.page_videos())
        active = self.guard.active_frames()
        top = sorted(cands, key=lambda c: score(c, active), reverse=True)[:5]
        t0 = time.time()
        covered, drm = probe_all([c for c in top if c.kind in ("hls", "dash")], self.ui.log)
        self.ui.log(f"find: {len(cands)} streams, measured {len(top)} in {time.time() - t0:.1f}s")
        self._check(attempt)
        drm_seen = bool(drm)
        pool = [c for c in top if not any(c is d for d in drm)]
        norm = lambda u: urlparse(u)._replace(query="", fragment="").geturl()     # tokens in the query differ per request
        covered = {norm(u) for u in covered}
        for c in pool:
            c.covered = norm(c.url) in covered
        decision = choose(pool, active, unsupported=bool(self.guard.snapshot()["unsupported"]))
        if not decision.best:
            if drm_seen:
                raise CastError("That video is DRM-protected, which CapoCast won't cast.\n\n"
                                "Use the service's own Roku channel instead.")
            self._post(lambda: self._note("No stream seen; asking yt-dlp…", self.ui.AMBER, 60))
            fallback = ytdlp_candidate(page_url)
            self._check(attempt)
            if not fallback:
                if decision.unsupported:
                    raise CastError("This site streams in a format CapoCast can't relay yet (it handles HLS, DASH "
                                    "and MP4, not WebM or MKV).")
                raise CastError("No video found yet. Press play on the video, then Cast.")
            decision.best = fallback
        if decision.close and not decision.playing:
            # Several streams fit and none is being played, so there's nothing to tell them apart by. Asking you to
            # pick from a list you can't judge is worse than a second of play: the player's own downloads decide it.
            raise CastError("I can't tell which stream is the video yet.\n\nPress play on the video, let it run for a "
                            "few seconds, then press Cast.")
        if decision.close:
            options = [decision.best] + decision.close
            self._post(lambda: self._pick(attempt, options, title))
            return
        self._send(attempt, decision.best, title)

    def _pick(self, attempt, options, title):
        """Two or more streams look equally like the video: let the user choose."""
        if attempt != self.attempt:
            return
        ui = self.ui
        win = tk.Toplevel(self.app)
        win.title("Which one is the video?")
        getattr(ui, "dark_titlebar", lambda w: None)(win)
        win.configure(bg=ui.BG)
        win.transient(self.app)
        win.resizable(False, False)
        tk.Label(win, text="More than one stream could be the video.\nPick the one that matches (length is the best clue).",
                 bg=ui.BG, fg=ui.FG, font=(ui.FONT, 10), justify="left").pack(padx=20, pady=(16, 8), anchor="w")
        lb = tk.Listbox(win, width=70, height=len(options), bg=ui.CARD, fg=ui.FG, selectbackground=ui.ACCENT,
                        relief="flat", font=(ui.FONT, 10), activestyle="none", highlightthickness=1,
                        highlightbackground=ui.EDGE)
        for c in options:
            lb.insert("end", c.label())
        lb.selection_set(0)
        lb.pack(padx=20)

        def done(choice):
            win.destroy()
            if choice is None:
                self.busy = False
                return self._note("")
            threading.Thread(target=self._guarded, args=(attempt, self._send, choice, title), daemon=True).start()

        row = tk.Frame(win, bg=ui.BG)
        row.pack(pady=16)
        ui.RoundButton(row, "Cast", lambda: done(options[(lb.curselection() or (0,))[0]]), width=110, height=36).pack(
            side="left", padx=6)
        cancel = ui.RoundButton(row, "Cancel", lambda: done(None), width=110, height=36)
        cancel.style("Cancel", "#26262b", "#34343a")
        cancel.pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: done(None))
        win.grab_set()

    def _ensure_relay(self, host):
        try:
            ip = local_ip_for(host)
        except OSError as e:
            raise CastError(f"Can't find this PC's network address to reach the Roku ({e}).")
        if self.relay and self.relay.server and self.relay.ip == ip:
            return self.relay
        if self.relay:
            self.relay.stop()
        self.relay = MediaRelay(ip, allow=lambda: self.app._host, log=self.ui.log)
        try:
            self.relay.start()
        except OSError as e:
            self.relay = None
            raise CastError(f"Couldn't start the stream server: {e}")
        return self.relay

    def _send(self, attempt, cand, title):
        """Worker thread: hand the stream to the relay, tell the Roku to play it, and confirm it arrived. If the cast
        fails or is cancelled after the encoder started, the encoder stops too: left alone it would keep the GPU busy
        until it was minutes ahead of a TV that never got the stream."""
        try:
            self._send_cast(attempt, cand, title)
        except BaseException:
            if self.relay and getattr(self, "upscale_owner", None) == attempt:
                self.upscale_owner, self.upscaled = None, None
                self.relay.stop_upscaling()
            raise

    def _send_cast(self, attempt, cand, title):
        self._check(attempt)
        ui, host = self.ui, self.app._host
        relay = self._ensure_relay(host)
        relay.stop_upscaling()                            # whatever was being made for the last cast
        self.upscaled = None
        subs = self._prepare_subs(attempt, cand)
        plan = self._upscale_plan(cand, subs)
        self._check(attempt)
        # Start where the page already is, not at 0:00. The TV takes a few seconds to start, so aim a little ahead.
        page_t = self.guard.video_time(cand.duration)
        start = (page_t + (self.sync.lead if self.sync else 3.0)) if ((cand.kind == "hls" or plan) and not cand.live
                                                                    and page_t and page_t > 5) else 0.0
        self._fresh_channel(host)
        self._check(attempt)
        url = self._start_upscale(attempt, relay, cand, start, plan) if plan else None
        if not url:
            url = relay.add(cand.url, cand.headers, cand.kind, start)
        sess = relay.session_for(url)
        ui.log(f"cast {cand.kind} {cand.url[:140]} -> {url} (from {start:.0f}s)")
        self._post(lambda: self._note("Sending to the TV…", ui.AMBER, 60))
        try:
            ui.Remote.cast_url(host, url)
        except Exception as e:
            raise CastError(str(e))
        self._post(self.app.autocast_off)             # don't let YouTube's auto-cast take the TV back
        try:
            self._await_tv(attempt, sess)
        except Cancelled:
            self._tv_back()                           # the TV had already been told to play this
            raise
        label, self.cast_site, self.cast_image = self._describe(title, cand)
        self.cast_next = self._find_next()
        self._progress_at = 0.0
        # From here the page's video is a muted remote for the TV: pause, play and scrubbing follow.
        if self.sync is None:
            self.sync = WebSync(self.guard, self.tv, self._relay_add, self.ui.log, self._following_stopped)
        self.sync.guard = self.guard                  # Brave may have been restarted since the last cast
        self.sync.start(cand, offset=sess.actual_start or 0.0, page_time=page_t, title=label,
                        restartable=bool(self.upscaled))
        self._post(lambda: self._cast_done(attempt, label))
        self._watch_health(attempt, sess)

    def _find_next(self):
        """The address of the episode after the one just cast, or '' if the page doesn't say. Never raises: this is a
        convenience, and a cast must not fail because of it."""
        try:
            return next_url(self.cast_page, self.guard.page_links(2.0)) or ""
        except Exception as e:
            self.ui.log(f"next episode lookup: {e!r}")
            return ""

    def _describe(self, title, cand):
        """(name, site, thumbnail) for what's now on the TV. The page's live title is used if it is still the page that
        was cast from (a single-page site changes it as you move between episodes); the title seen when Cast was
        pressed otherwise."""
        info = self.guard.page_info(2.0)
        defrag = lambda u: urlparse(u or "")._replace(fragment="").geturl()
        same = not info or defrag(info.get("url")) == defrag(self.cast_page)
        live = (info.get("title") or info.get("og_title") or "").strip() if info and same else ""
        site = discord_presence.site_label(self.cast_page, info.get("site", "") if info and same else "")
        name = discord_presence.clean_title(live or title, site, self.cast_page)
        image = ""
        if same:
            found = self.guard.page_images(2.0, cand.duration)
            image = discord_presence.pick_image(found, log=self.ui.log)
            self.ui.log(f"discord thumbnail: {image or 'none'} (page offered {len(found)})")
        return name or urlparse(cand.url).hostname or "video", site, image

    def _upscale_plan(self, cand, subs=""):
        """What the PC makes for this cast: {'mode', 'duration', 'audio', 'subs'}, or None to cast it as it is (saying
        why, so a switch being on and the TV looking the same is never a mystery). The mode is the Upscale setting; with
        subtitles chosen and no upscaling they're drawn onto the video at its own size ('burn')."""
        hdr = self.hdr
        mode = self.upscale if self.upscale != "off" else (upscaler.BURN if (subs or hdr) else "off")
        if mode == "off":
            return None
        ui, burn = self.ui, mode == upscaler.BURN
        why = upscaler.missing(mode, bool(subs), hdr)
        if why and (subs or hdr) and not burn:
            ui.log(f"upscale unavailable ({why}); trying {'subtitles' if subs else 'HDR'} alone")
            mode, burn = upscaler.BURN, True
            why = upscaler.missing(mode, bool(subs), hdr)
            if why and subs and hdr:
                hdr = False                                   # no libplacebo: the subtitles don't need it
                why = upscaler.missing(mode, True)
        what = "subtitles" if subs else "HDR"                 # what a burn-only cast is adding
        if not why and cand.live:
            why = f"Live streams can't have {what} added" if burn else "Live streams aren't upscaled"
        info = None
        if not why and (hdr or not cand.duration or not cand.height or not cand.width or cand.has_audio is None):
            self._post(lambda: self._note("Reading the video…", ui.AMBER, 30))
            info = upscaler.probe(cand.url, cand.headers, cand.kind, ui.log)
        duration = cand.duration or (info or {}).get("duration") or 0
        height = cand.height or (info or {}).get("height") or 0
        width = cand.width or (info or {}).get("width") or 0
        audio = cand.has_audio if cand.has_audio is not None else (info or {}).get("audio", True)
        if not why and not duration:
            why = (f"Can't add {what} to this one" if burn else "Can't upscale this one") + ": its length isn't known"
        if not why and cand.has_video is False:
            why = "This stream has no picture to " + (f"add {what} to" if burn else "upscale")
        if not why and hdr and (info or {}).get("transfer") in upscaler.HDR_TRANSFERS:
            hdr = False                                           # already HDR: lifting it again would wreck it
            if burn and not subs:
                why = "This video is already HDR"
        if not why and not burn and upscaler.already_4k(width, height):
            if subs or hdr:
                mode, burn = upscaler.BURN, True                  # already 4K: no upscaling, but the extras still go on
            else:
                why = "This video is already 4K"
        if why:
            ui.log(f"{what if burn else 'upscale'} skipped: {why}")
            self._post(lambda: self._note(why + ", casting it as it is", ui.AMBER, 8))
            return None
        return {"mode": mode, "duration": duration, "audio": bool(audio), "subs": subs or None, "hdr": hdr}

    def _start_upscale(self, attempt, relay, cand, start, plan):
        """Start the encoder and wait for its first piece. Returns the URL for the TV, or None to cast plainly."""
        ui = self.ui
        extras = " and ".join(n for n, on in (("subtitles", plan.get("subs")), ("HDR", plan.get("hdr"))) if on)
        doing = (f"Adding {extras}" if plan["mode"] == upscaler.BURN
                 else f"Upscaling ({UPSCALE_NAMES[plan['mode']]})" + (f" with {extras}" if extras else ""))
        self._post(lambda: self._note(doing + "…", ui.AMBER, 60))
        self.upscale_owner = attempt                      # so a failure of this attempt (and only this one) stops it
        url = relay.add_upscaled(cand.url, cand.headers, cand.kind, start, plan["duration"], plan["mode"], plan["audio"],
                                 plan.get("subs"), plan.get("hdr", False))
        sess = relay.session_for(url)
        try:
            ready = sess.wait_ready(lambda: self._check(attempt))
        except Cancelled:
            relay.stop_upscaling()
            raise
        if ready:
            self.upscaled = {**plan, "session": sess}
            return url
        ui.log(f"upscale failed, casting plainly: {sess.error}")
        relay.stop_upscaling()
        failed = f"Adding {extras}" if plan["mode"] == upscaler.BURN else "Upscaling"
        self._post(lambda: self._note(f"{failed} failed, casting it as it is", ui.AMBER, 8))
        return None

    def _await_tv(self, attempt, sess):
        """Wait for the TV to fetch the stream and, for a trimmed HLS stream, to learn where it begins."""
        deadline = time.time() + 20
        while time.time() < deadline and not sess.hits and not sess.error:
            self._check(attempt)
            time.sleep(0.4)
        if sess.error:
            raise CastError(sess.error)
        if not sess.hits:
            raise CastError(
                "The TV never asked this PC for the video.\n\nWindows Firewall is the usual cause: when Windows asks, "
                "allow CapoCast (or Python) on private networks. Also check the Roku IP in the Listen tab.")
        end = time.time() + 8
        while sess.actual_start is None and time.time() < end:
            self._check(attempt)
            time.sleep(0.2)
        self._check(attempt)

    def _fresh_channel(self, host):
        """Recasting while the RokuCast channel is still up from the last cast hands the new stream to a player that's
        mid-way through the old one, and the TV can come back with no sound. Back out of the channel first so every
        cast starts on a clean player."""
        if self.ui.Remote.active_app(host) != "dev":
            return
        try:
            self.tv.key("Back")
        except Exception as e:
            self.ui.log(f"fresh start: Back failed: {e!r}")
            return
        end = time.time() + 4
        while time.time() < end and self.ui.Remote.active_app(host) == "dev":
            time.sleep(0.25)
        time.sleep(0.6)

    FAIL_WINDOW, FAIL_LIMIT = 30.0, 3        # this many failed requests within this many seconds is a stream going bad

    def _watch_health(self, attempt, sess, tick=2.0):
        """For as long as this cast is followed, notice if the stream is quietly failing (segments refused or
        expired, which is how a cast ends up silent or frozen part-way), if the upscaler dies, or if it can't keep up.
        A seek moves the cast to a new session, which is followed too."""
        self.cast_session = sess
        current, seen, fails, warned_at, slow = sess, 0, [], 0.0, False
        while self.sync is not None and self.sync.active and attempt == self.attempt:
            time.sleep(tick)
            sess = self.cast_session or sess
            if sess is not current:                           # a scrub restarted the stream
                current, seen, fails, slow = sess, 0, [], False
            now = time.time()
            fresh = list(sess.failures[seen:])
            seen += len(fresh)
            fails = [(t, c) for t, c in fails if now - t < self.FAIL_WINDOW] + [(now, c) for c, _ in fresh]
            if len(fails) >= self.FAIL_LIMIT and now - warned_at > 60:
                warned_at = now
                codes = sorted({c for _, c in fails})
                self.ui.log(f"stream failing: {sess.failures[-6:]}")
                self._post(lambda: self._note(
                    "Parts of this stream are failing to load" + (f" (HTTP {codes[0]})" if codes[0] else "")
                    + ". Reload the page and Cast again.", self.ui.AMBER, 20))
            elif sess.error and now - warned_at > 60 and sess.error != getattr(self, "_reported_error", ""):
                warned_at, self._reported_error = now, sess.error
                reason = sess.error
                self._post(lambda: self._note("The stream stopped: " + reason, self.ui.AMBER, 20))
            if not slow and getattr(sess, "stalls", 0) >= 3 and self.upscaled:
                slow = True                                   # a seek swaps the session in the plan, so this follows it
                if (self.upscaled or {}).get("mode") == upscaler.BURN:
                    extras = " and ".join(n for n, on in (("subtitles", self.upscaled.get("subs")),
                                                          ("HDR", self.upscaled.get("hdr"))) if on)
                    msg = (f"Adding the {extras} is running slower than the video, so the TV may buffer. The site may "
                           "be slow, or the GPU busy.")
                else:
                    msg = ("Upscaling is running slower than the video, so the TV may buffer. The site may be slow, or "
                           "the GPU busy: try a lighter Upscale setting.")
                self._post(lambda: self._note(msg, self.ui.AMBER, 20))

    def _relay_add(self, url, headers, kind, start):
        relay = self._ensure_relay(self.app._host)
        if self.upscaled:
            plan = self.upscaled
            public = relay.add_upscaled(url, headers, kind, start, plan["duration"], plan["mode"], plan["audio"],
                                        plan.get("subs"), plan.get("hdr", False))
            plan["session"] = relay.session_for(public)
            # The TV isn't told about the new stream until its first piece exists. A restarted encoder has to open the
            # site and start up again, and a TV that asks sooner gets nothing, gives up, and shows a playback error.
            wait = getattr(plan["session"], "wait_ready", None)
            if wait:
                wait(lambda: None, 40.0)
        else:
            public = relay.add(url, headers, kind, start)
        self.cast_session = relay.session_for(public)
        return public, self.cast_session

    def _cast_done(self, attempt, label):
        if attempt != self.attempt:
            return
        self.busy = False
        # Discord's clock reads the position in the video: it started `offset` seconds in when the TV's stream began,
        # and with the length known it counts down what's left. tv_state() keeps it true from here on.
        now, sync = time.time(), self.sync
        live = bool(sync and sync.live)
        self.cast_since = now - (0.0 if live else float(getattr(sync, "offset", 0) or 0))
        dur = float(getattr(sync, "dur", 0) or 0)
        self.cast_end = self.cast_since + dur if dur and not live else None
        self._set_presence(label, self.cast_image, self.cast_site)
        self.next.arm(self.cast_page)
        self._note("On TV  ·  " + (label if len(label) <= 52 else label[:51] + "…") + "  ·  following this page",
                   self.ui.GREEN, 20)

    def close(self):
        """Shut the browser down cleanly so it saves the profile (sign-ins), then make sure it's gone."""
        if self.sync:
            self.sync.stop("", quiet=True)
        try:
            self.guard.close_browser()
            time.sleep(0.6)
        except Exception:
            pass
        self.guard.stop()
        if self.sub_dir:
            shutil.rmtree(self.sub_dir, ignore_errors=True)
        if self.relay:
            self.relay.stop()
        if self.proc is not None:
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.terminate()
            self.proc = None
