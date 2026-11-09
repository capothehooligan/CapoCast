"""Watches and protects the Web tab's browser (Brave) over the Chrome DevTools Protocol.

What it does, all on one background thread so nothing here needs locks beyond the snapshot the UI reads:
  * closes pop-ups and pop-unders the moment they open (same-site ones open in the main view instead),
  * stops scripted cross-site redirects that didn't follow a real click,
  * removes invisible click-trap layers laid over a page,
  * denies downloads and notification prompts,
  * records every video stream the page loads (with the headers the browser used) for the Cast button.

Brave's Shields block the ad requests themselves; this layer handles what Shields can't (windows, redirects,
overlays) and finds the real video among the decoys.
"""
import base64
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

from websockets.sync.client import connect

import subtitles
from discord_presence import looks_like_logo
from media_relay import replay_headers
from stream_finder import SNIFF_MIMES, Candidate, classify, host_of, is_ad_url, site_of, sniff

LOST = "Lost contact with the browser"   # guard.error when the DevTools link dropped while Brave may still be running
GESTURE_WINDOW = 1.5     # seconds a real click/keypress "covers" a navigation that follows it
POPUP_GESTURE = 3.0
MAX_CANDIDATES = 40
MAX_REQUESTS = 1500

# Injected into every frame before the page's own scripts. It reports real clicks, what each <video> is doing,
# and removes transparent full-page layers (click traps). It never touches pages that contain a <video>, because
# players use transparent layers of their own.
PAGE_JS = r"""
(() => {
  if (window.__rlGuard) return;
  Object.defineProperty(window, '__rlGuard', {value: 1});
  const call = (name, arg) => { try { window[name](arg); } catch (e) {} };
  const gesture = (e) => { if (e.isTrusted) call('rlGesture', '1'); };
  addEventListener('pointerdown', gesture, true);
  addEventListener('keydown', gesture, true);

  let last = '', ticks = 0;
  const report = () => {
    const out = [];
    for (const v of document.querySelectorAll('video')) {
      const r = v.getBoundingClientRect();
      const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
      const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
      out.push({src: v.currentSrc || v.src || '', dur: isFinite(v.duration) ? v.duration : 0,
                playing: !v.paused && !v.ended && v.readyState > 2, paused: v.paused, area: Math.round(w * h),
                t: v.currentTime,
                muted: v.muted, poster: v.poster || '',
                tracks: [...v.querySelectorAll('track')].filter(t => t.src).map(t => ({
                  src: t.src, label: t.label || '', lang: t.srclang || '', kind: t.kind || 'subtitles', def: t.default}))});
    }
    const s = JSON.stringify(out.map(o => ({...o, t: 0})));
    // report changes at once, and re-send every ~2 s so the app can tell "still playing" from "gone"
    if (s !== last || (out.length && ++ticks % 3 === 0)) { last = s; call('rlVideo', JSON.stringify(out)); }
  };

  const trap = (el) => {
    if (el.__rl) return false;
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'absolute') return false;
    if (cs.pointerEvents === 'none' || cs.visibility === 'hidden' || cs.display === 'none') return false;
    const r = el.getBoundingClientRect();
    if (r.width < innerWidth * 0.8 || r.height < innerHeight * 0.8) return false;
    if (el.querySelector('video,iframe,canvas,input,button,form,img,svg')) return false;
    if ((el.innerText || '').trim().length > 0) return false;
    if (cs.backgroundImage && cs.backgroundImage !== 'none') return false;
    const bg = cs.backgroundColor;
    return bg === 'transparent' || /rgba\(.*,\s*0\)$/.test(bg) || parseFloat(cs.opacity) < 0.05;
  };
  const sweep = () => {
    if (document.body && !document.querySelector('video')) {
      for (const el of document.body.querySelectorAll('div,a,span,section')) {
        if (trap(el)) { el.__rl = 1; el.remove(); call('rlBlocked', 'overlay'); }
      }
    }
    report();
  };
  setInterval(sweep, 600);

  // A normal same-site link that asks for a new tab opens in this view instead (there are no tabs here).
  addEventListener('click', (e) => {
    if (!e.isTrusted || window !== top) return;
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || (a.target !== '_blank' && a.target !== '_new')) return;
    try {
      const u = new URL(a.href, location.href);
      if (/^https?:$/.test(u.protocol) && u.hostname === location.hostname) { e.preventDefault(); location.href = u.href; }
    } catch (x) {}
  }, true);

  // ---- the phone's YouTube tab (the app sets the rl_yt cookie on youtube.com while that tab is open) ----
  // A tap on a video plays it on the TV through Playlet instead of opening it here, and the page is drawn black.
  const ytOn = () => /(^|; )rl_yt=1/.test(document.cookie);
  const ytWatch = (href) => {
    try {
      const u = new URL(href, location.href), h = u.hostname;
      if (h === 'youtu.be') return u.pathname.length > 1 ? u.href : '';
      if (!/(^|\.)youtube\.com$/.test(h)) return '';
      if (u.pathname === '/watch' && u.searchParams.get('v')) return u.href;
      if (/^\/(shorts|live)\/[\w-]{6,}/.test(u.pathname)) return u.href;
    } catch (x) {}
    return '';
  };
  addEventListener('click', (e) => {
    if (!e.isTrusted || window !== top || !ytOn()) return;
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    const w = a && ytWatch(a.href);
    if (!w) return;
    e.preventDefault(); e.stopImmediatePropagation();
    call('rlYoutube', w);
  }, true);
  if (window === top && /(^|\.)youtube\.com$/.test(location.hostname) && ytOn()) {
    const paint = () => {
      if (document.getElementById('rl-black') || !document.documentElement) return;
      const css = document.createElement('style');
      css.id = 'rl-black'; css.textContent = __YT_BLACK__;
      document.documentElement.appendChild(css);
    };
    paint();
    document.addEventListener('DOMContentLoaded', paint);
  }

  // ---- is a text field focused? (the phone pops its typing bar up when one is) ----
  if (window === top) {
    const editable = (el) => {
      if (!el || el.nodeType !== 1) return false;
      if (el.isContentEditable) return true;
      if (el.tagName === 'TEXTAREA') return !el.readOnly && !el.disabled;
      if (el.tagName !== 'INPUT' || el.readOnly || el.disabled) return false;
      return /^(|text|search|url|email|password|tel|number)$/i.test(el.getAttribute('type') || '');
    };
    let was = '';
    const report = () => {
      const now = editable(document.activeElement) ? '1' : '0';
      if (now !== was) { was = now; call('rlEdit', now); }
    };
    addEventListener('focusin', () => setTimeout(report, 0), true);
    addEventListener('focusout', () => setTimeout(report, 0), true);
  }

  // ---- mirroring: while a Web cast plays on the TV, this page's video is a muted remote control for it ----
  let mirror = false, ignoreUntil = 0;
  const muted = new WeakSet();
  const vids = () => [...document.querySelectorAll('video')];
  const emit = (kind, v) => {
    if (Date.now() < ignoreUntil) return;              // our own correction, not something you did
    call('rlEvent', JSON.stringify({kind, t: v.currentTime, paused: v.paused,
                                    dur: isFinite(v.duration) ? v.duration : 0, src: v.currentSrc || v.src || ''}));
  };
  for (const k of ['play', 'pause', 'seeked', 'ended']) {
    document.addEventListener(k, (e) => { if (e.target && e.target.tagName === 'VIDEO') emit(k, e.target); }, true);
  }
  const applyMute = () => {
    for (const v of vids()) {
      if (mirror) { if (!v.muted) { v.muted = true; muted.add(v); } }
      else if (muted.has(v)) { v.muted = false; muted.delete(v); }
    }
  };
  setInterval(applyMute, 400);

  addEventListener('message', (e) => {
    const m = e.data;
    if (!m || !m.__rl) return;
    if (m.__rl === 'pause') { for (const v of vids()) { try { v.pause(); } catch (x) {} } }
    else if (m.__rl === 'mirror') { mirror = !!m.on; applyMute(); }
    else if (m.__rl === 'toggle' || m.__rl === 'skip') {
      // what pressing the player's own buttons does (so the TV follows it); not marked as our own correction
      const mine = vids().filter(v => v.readyState > 0 && (!m.dur || Math.abs(v.duration - m.dur) < 3));
      if (m.__rl === 'toggle') {
        const playing = mine.some(v => !v.paused && !v.ended);
        for (const v of mine) { try { if (playing) v.pause(); else v.play(); } catch (x) {} }
      } else {
        for (const v of mine) { try { v.currentTime = Math.max(0, Math.min(isFinite(v.duration) ? v.duration - 1 : 1e9, v.currentTime + m.dt)); } catch (x) {} }
      }
    }
    else if (m.__rl === 'seek') {
      for (const v of vids()) {
        if (!m.dur || Math.abs(v.duration - m.dur) < 3) {
          ignoreUntil = Date.now() + 1500;                 // our own move: its seek / pause / play events aren't you
          try { v.currentTime = m.t; } catch (x) {}
          if (m.paused === true) { try { v.pause(); } catch (x) {} }        // held while the TV buffers
          else if (m.paused === false) { v.play().catch(() => {}); }
        }
      }
    }
    for (let i = 0; i < frames.length; i++) { try { frames[i].postMessage(m, '*'); } catch (x) {} }
  });
})();
"""


# YouTube's own dark theme is dark grey; the phone's YouTube tab pushes it to true black (kind to an OLED screen)
YT_BLACK_CSS = """
html, html[dark], [dark] {
  --yt-spec-base-background: #000 !important; --yt-spec-general-background-a: #000 !important;
  --yt-spec-general-background-b: #000 !important; --yt-spec-general-background-c: #000 !important;
  --yt-spec-raised-background: #0a0a0a !important; --yt-spec-menu-background: #0a0a0a !important;
  --yt-spec-additive-background: #1a1a1a !important; --yt-spec-badge-chip-background: #1a1a1a !important;
  --yt-spec-brand-background-solid: #000 !important; --yt-spec-brand-background-primary: #000 !important;
}
html, body, ytd-app, ytm-app, #page-manager, ytd-browse, .page-container, ytm-browse, ytm-mobile-topbar-renderer,
ytm-pivot-bar-renderer, #masthead-container, ytd-masthead, #background { background: #000 !important; }
"""
PAGE_JS = PAGE_JS.replace("__YT_BLACK__", json.dumps(YT_BLACK_CSS))


def dark_pref(cookies):
    """The PREF cookie value that turns YouTube's dark theme on, keeping whatever else the cookie already says.
    `cookies` is the list Network.getCookies returns. The theme is two bits of PREF's f6 flags (hex)."""
    value = next((c.get("value", "") for c in cookies if c.get("name") == "PREF"), "")
    parts = [p for p in value.split("&") if p and not p.startswith("f6=")]
    old = next((p[3:] for p in value.split("&") if p.startswith("f6=")), "")
    try:
        flags = int(old, 16) if old else 0
    except ValueError:
        flags = 0
    parts.append("f6=%x" % ((flags & ~0x80000) | 0x400))      # 0x400 = dark, 0x80000 = light
    return "&".join(parts)


def _total_size(headers):
    """Whole-file size from Content-Range ('bytes 0-1/12345') or, failing that, Content-Length."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    cr = h.get("content-range", "")
    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
        return int(cr.rsplit("/", 1)[1])
    cl = h.get("content-length", "")
    return int(cl) if cl.isdigit() else 0


def normalize_address(text, home="https://duckduckgo.com"):
    """What you typed in the address box -> a URL (links as-is, 'site.com' gets https, anything else is a search)."""
    text = (text or "").strip()
    if not text:
        return home
    p = urllib.parse.urlparse(text)
    if p.scheme in ("http", "https"):
        return text
    first = text.split("/")[0]
    if "." in first and " " not in text:
        return "https://" + text
    return "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(text)


class WebGuard:
    def __init__(self, log=lambda m: None, home=None):
        self.log = log
        self.home = home
        self.debug = bool(os.environ.get("RL_DEBUG"))
        self._lock = threading.Lock()
        self._cmds = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self.ws = None
        self._id = 0
        self._callbacks = {}
        self.running = False
        self.error = ""
        self.attached_at = 0.0
        # session bookkeeping (reader thread only)
        self._baseline, self._ready = set(), False
        self._sessions = {}              # sessionId -> {"type", "targetId"}
        self._main = None                # sessionId of the page
        self._main_target = None
        self._main_frame = None
        self._ctx_frame = {}             # (sessionId, executionContextId) -> frameId
        self._reqs = {}                  # (sessionId, requestId) -> {"url","headers","frame"}
        self._sniffing = {}              # requests whose body we'll read to see if it's a hidden playlist
        self._subs = {}                  # separate subtitle files the page has fetched: url -> {url, headers, frame}
        self._activity = {}              # host -> when the page last downloaded video pieces from it
        self._last_gesture = 0.0
        self.on_youtube = None           # called with the address of a YouTube video tapped in the phone's YouTube tab
        self._yt_dark = False            # YouTube's theme cookie has been set in this browser
        self._editing = False            # a text field has focus on the page
        self._allowed = {}               # site -> expiry time ("Allow once")
        # shared with the UI (guarded by _lock)
        self.page_events = queue.Queue(maxsize=300)   # play / pause / seeked / ended from the page's videos
        self.epoch = 0                   # counts main-page changes, so a cast can tell when you've left its page
        self._view = None                # (width, height, on): what the app wants drawn
        self._frame_seq, self._frame, self.frame_meta = 0, b"", {}
        self._phone = None               # (width, height, last asked) while a phone is browsing
        self._ua = None                  # (userAgent, metadata) presented to sites instead of "HeadlessChrome"
        self._url, self._title, self._page_key = "", "", ""
        self._cands = {}                 # url -> Candidate
        self._videos = {}                # (sessionId, frameId) -> {"at", "items"}
        self._unsupported = ""
        self._arrivals = 0
        self.stats = {"popups": 0, "redirects": 0, "overlays": 0, "ads": 0}
        self.last_block = None           # {"what", "url"}

    # ---- lifecycle ------------------------------------------------------------
    def attach(self, port_file):
        """Connect to the browser named by its DevToolsActivePort file and start the guard thread."""
        with open(port_file) as f:
            port = f.read().split()[0]
        ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5))
        # No keepalive pings: this is a loopback socket, which can't go silently dead, and on a PC busy with a game and a
        # 4K encode a late pong would close a perfectly good connection.
        self.ws = connect(ver["webSocketDebuggerUrl"], max_size=None, open_timeout=5, ping_interval=None)
        self._stop.clear()
        self.running, self.error, self.attached_at = True, "", time.time()
        self._thread = threading.Thread(target=self._run, name="web-guard", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ---- UI-facing (any thread) -------------------------------------------------
    def snapshot(self):
        with self._lock:
            return {"running": self.running, "error": self.error, "url": self._url, "title": self._title,
                    "stats": dict(self.stats), "streams": sum(1 for c in self._cands.values()),
                    "last_block": dict(self.last_block) if self.last_block else None,
                    "unsupported": self._unsupported, "editing": self._editing}

    def candidates(self):
        now = time.time()
        with self._lock:
            current = [c for c in self._cands.values() if not c.stale] or self._cands.values()
            out = [Candidate(**{**c.__dict__, "headers": dict(c.headers)}) for c in current]
        for c in out:
            c.active_age = now - self._activity.get(host_of(c.url), -1e9) if host_of(c.url) in self._activity else 1e9
        return out

    def has_fresh_stream(self):
        """A stream has been requested since the page last changed its address."""
        with self._lock:
            return any(not c.stale for c in self._cands.values())

    def page_videos(self):
        now = time.time()
        with self._lock:
            return [v for e in self._videos.values() if now - e["at"] < 5 for v in e["items"]]

    def active_frames(self):
        """Frames whose <video> is playing and (nearly) the biggest one on screen."""
        now = time.time()
        with self._lock:
            best, rows = 0, []
            for (_, frame), e in self._videos.items():
                if now - e["at"] > 5:
                    continue
                for v in e["items"]:
                    if v.get("playing"):
                        rows.append((frame, v.get("area", 0)))
                        best = max(best, v.get("area", 0))
            return {f for f, a in rows if a >= best * 0.5}

    def navigate(self, url):
        self._cmds.put(("navigate", url))

    YT_LEASE = 15.0                      # seconds the YouTube tab's switch stays on without the phone renewing it

    def youtube_mode(self, on, go=None):
        """The phone's YouTube tab is open (the phone renews this every few seconds; it lapses by itself): videos tapped
        on youtube.com go to `on_youtube` instead of playing here, and YouTube is drawn black. `go` also opens that
        page, after the theme is in place."""
        self._cmds.put(("youtube", (bool(on), go)))

    def _youtube(self, on, go, s):
        def finish(cookies):
            lease = time.time() + self.YT_LEASE if on else 1
            self._send("Network.setCookie", {"name": "rl_yt", "value": "1", "domain": ".youtube.com", "path": "/",
                                             "secure": True, "expires": lease}, s)
            if cookies is not None:
                self._send("Network.setCookie", {"name": "PREF", "value": dark_pref(cookies), "domain": ".youtube.com",
                                                 "path": "/", "secure": True, "expires": time.time() + 400 * 86400}, s)
            if go:
                self._send("Page.navigate", {"url": go}, s)
            elif cookies is not None and host_of(self._url).endswith("youtube.com"):
                self._send("Page.reload", {}, s)              # already on YouTube: redraw it in the new theme

        if on and (go or not self._yt_dark):
            self._yt_dark = True
            self._send("Network.getCookies", {"urls": ["https://www.youtube.com/"]}, s,
                       cb=lambda r: finish(r.get("cookies", [])))
        else:
            finish(None)

    def back(self):
        self._cmds.put(("history", -1))

    def forward(self):
        self._cmds.put(("history", 1))

    def reload(self):
        self._cmds.put(("reload", None))

    def pause_page(self):
        self.post_page({"__rl": "pause"})

    def post_page(self, message):
        """Send a message to every frame's script (it relays it to nested frames)."""
        self._cmds.put(("post", message))

    def mirror(self, on):
        """On: the page's video is muted and reports play / pause / seek (see WebSync). Off: sound comes back."""
        self.post_page({"__rl": "mirror", "on": bool(on)})

    def toggle_page(self, dur=0):
        """Play or pause the page's video, as pressing its own button would (WebSync then moves the TV)."""
        self.post_page({"__rl": "toggle", "dur": float(dur or 0)})

    def skip_page(self, dt, dur=0):
        """Jump the page's video forward or back by dt seconds, as scrubbing it would."""
        self.post_page({"__rl": "skip", "dt": float(dt), "dur": float(dur or 0)})

    def seek_page(self, t, dur, paused=None):
        """Move the page's copy of the video (the one that's `dur` seconds long) to t, without it counting as input.
        paused=True also pauses it there (held while the TV buffers), False also plays it."""
        msg = {"__rl": "seek", "t": float(t), "dur": float(dur or 0)}
        if paused is not None:
            msg["paused"] = bool(paused)
        self.post_page(msg)

    def video_time(self, dur=0):
        """Where the page's video is now (seconds), or None. dur picks the right one when a page has several."""
        now = time.time()
        with self._lock:
            for e in self._videos.values():
                if now - e["at"] > 6:
                    continue
                for v in e["items"]:
                    if v.get("dur") and (not dur or abs(v["dur"] - dur) < 3):
                        return v["t"] + (now - e["at"] if v.get("playing") else 0.0)
        return None

    IMAGE_JS = r"""(() => {
      const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
      const meta = [], hero = [];
      const add = (list, u) => { u = typeof u === 'string' ? abs(u.trim()) : ''; if (u && !list.includes(u)) list.push(u); };
      // every preview tag, not just the first: single-page sites leave the site's own icon in the first one
      for (const sel of ['meta[property="og:image:secure_url"]', 'meta[property="og:image"]', 'meta[name="twitter:image"]',
                         'meta[name="twitter:image:src"]', 'link[rel~="image_src"]'])
        for (const m of document.querySelectorAll(sel)) add(meta, m.content || m.href || '');
      const walk = (v) => { if (typeof v === 'string') add(meta, v); else if (Array.isArray(v)) v.forEach(walk);
                            else if (v && typeof v === 'object') walk(v.url || v.contentUrl || ''); };
      for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try { const j = JSON.parse(s.textContent); for (const n of (Array.isArray(j) ? j : [j])) walk(n.thumbnailUrl || n.image); } catch (e) {}
      }
      // last resort: the big picture near the top of the page (a hero backdrop or poster)
      const big = [...document.images].map(i => ({i, r: i.getBoundingClientRect()}))
        .filter(({i, r}) => i.naturalWidth >= 400 && i.naturalHeight >= 220 && r.width >= 300 && r.height >= 150
                            && r.top < 900 && r.bottom > 0)
        .sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height).slice(0, 3);
      for (const {i} of big) add(hero, i.currentSrc || i.src);
      return JSON.stringify({meta, hero});
    })()"""

    INFO_JS = r"""(() => {
      const meta = (sel) => { const m = document.querySelector(sel); return (m && m.content || '').trim(); };
      return JSON.stringify({url: location.href, title: document.title || '',
                             og_title: meta('meta[property="og:title"]'), site: meta('meta[property="og:site_name"]')});
    })()"""

    def page_info(self, timeout=2.0):
        """{'url', 'title', 'og_title', 'site'} of the page as it is right now (the live title, which a single-page site
        updates as you change episode), or {} if the browser doesn't answer in time. Never raises."""
        box, done = [], threading.Event()
        self._cmds.put(("eval", (self.INFO_JS, lambda r: (box.append(r), done.set()))))
        if done.wait(timeout) and box and isinstance(box[0], dict):
            try:
                info = json.loads((box[0].get("result") or {}).get("value") or "{}")
            except (TypeError, ValueError):
                return {}
            return {k: str(v) for k, v in info.items()} if isinstance(info, dict) else {}
        return {}

    LINKS_JS = r"""(() => {
      const out = [];
      for (const a of document.querySelectorAll('a[href]')) {
        const h = a.href;
        if (/^https?:/.test(h) && /\d/.test(h) && out.length < 400 && !out.includes(h)) out.push(h);
      }
      return JSON.stringify(out);
    })()"""

    def page_links(self, timeout=2.0):
        """The addresses of the page's links that contain a number (episode lists, 'next' buttons), or [] if there are
        none or the browser doesn't answer in time. Never raises."""
        box, done = [], threading.Event()
        self._cmds.put(("eval", (self.LINKS_JS, lambda r: (box.append(r), done.set()))))
        if done.wait(timeout) and box and isinstance(box[0], dict):
            try:
                links = json.loads((box[0].get("result") or {}).get("value") or "[]")
            except (TypeError, ValueError):
                return []
            return [str(x) for x in links if isinstance(x, str)] if isinstance(links, list) else []
        return []

    def page_images(self, timeout=2.0, dur=0):
        """Every picture the page offers for what's playing, best guess first: its preview tags (og:image, twitter:image,
        JSON-LD), then the poster of the video being cast, then the big picture near the top of the page. Logos and
        icons are left out. [] if there is nothing, or the browser doesn't answer in time. Never raises."""
        box, done = [], threading.Event()
        self._cmds.put(("eval", (self.IMAGE_JS, lambda r: (box.append(r), done.set()))))
        found = {}
        if done.wait(timeout) and box and isinstance(box[0], dict):
            try:
                found = json.loads((box[0].get("result") or {}).get("value") or "{}")
            except (TypeError, ValueError):
                found = {}
        posters, now = [], time.time()
        with self._lock:
            for e in self._videos.values():
                if now - e["at"] > 6:
                    continue
                for v in e["items"]:
                    poster = v.get("poster") or ""
                    if poster.startswith("https://") and (not dur or not v.get("dur") or abs(v["dur"] - dur) < 3):
                        posters.append(poster)
        out = []
        for url in [*(found.get("meta") or []), *posters, *(found.get("hero") or [])]:
            if isinstance(url, str) and url.startswith("https://") and not looks_like_logo(url) and url not in out:
                out.append(url)
        return out

    def page_image(self, timeout=2.0, dur=0):
        """The first picture page_images offers, or ''."""
        found = self.page_images(timeout, dur)
        return found[0] if found else ""

    def video_paused(self, dur=0):
        """Whether the page's video (the one `dur` seconds long) is paused: stopped on purpose, which a stall is not.
        None if it isn't there, or the page didn't say."""
        now = time.time()
        with self._lock:
            for e in self._videos.values():
                if now - e["at"] > 6:
                    continue
                for v in e["items"]:
                    if v.get("dur") and (not dur or abs(v["dur"] - dur) < 3):
                        return bool(v["paused"]) if "paused" in v else None
        return None

    def video_playing(self, dur=0):
        now = time.time()
        with self._lock:
            return any(v.get("playing") and (not dur or abs(v.get("dur", 0) - dur) < 3)
                       for e in self._videos.values() if now - e["at"] < 6 for v in e["items"])

    # The browser is headless: the app paints the page from these frames and sends input back.
    def set_view(self, width, height, on=True):
        """Tell the browser what size to render at, and whether to stream frames (off while the tab is hidden)."""
        self._view = (max(200, int(width)), max(120, int(height)), bool(on))
        self._cmds.put(("view", None))

    PHONE_IDLE = 8.0                          # let go of the phone's view after this many seconds of silence

    def phone_watch(self, width, height):
        """A phone is looking at the page: render at its size (and stream) until it stops asking."""
        w, h = max(240, min(int(width), 900)), max(240, min(int(height), 1600))
        cur = self._phone
        self._phone = (w, h, time.time())
        if not cur or cur[:2] != (w, h):
            self._cmds.put(("view", None))

    def phone_expire(self):
        """Back to the app's own size once the phone has gone quiet. Called now and then by the tab."""
        p = self._phone
        if p and time.time() - p[2] > self.PHONE_IDLE:
            self._phone = None
            self._cmds.put(("view", None))

    @property
    def phone_active(self):
        return self._phone is not None

    def view_size(self):
        """(width, height) in CSS pixels that the page is being rendered at right now."""
        p = self._phone
        return p[:2] if p else (self._view[:2] if self._view else (0, 0))

    def frame(self):
        """(sequence number, JPEG bytes) of the newest frame; the number changes when there's a new one."""
        with self._lock:
            return self._frame_seq, self._frame

    def mouse(self, kind, x, y, button="none", buttons=0, clicks=0, dx=0, dy=0, mods=0):
        self._cmds.put(("input", ("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": button, "buttons": buttons, "clickCount": clicks,
            "deltaX": dx, "deltaY": dy, "modifiers": mods})))

    def key(self, event):
        self._cmds.put(("input", ("Input.dispatchKeyEvent", event)))

    def text(self, text):
        self._cmds.put(("input", ("Input.insertText", {"text": text})))

    def close_browser(self):
        self._cmds.put(("close", None))

    def allow_last_block(self):
        """'Allow once': let the site we just blocked through, and go there."""
        with self._lock:
            lb = self.last_block
        if lb and lb.get("url"):
            self._cmds.put(("allow", lb["url"]))

    # ---- protocol plumbing (reader thread) ------------------------------------------
    def _send(self, method, params=None, session=None, cb=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        if cb:
            self._callbacks[self._id] = cb
        self.ws.send(json.dumps(msg))

    def _run(self):
        try:
            self._start()
            while not self._stop.is_set():
                self._drain()
                try:
                    raw = self.ws.recv(timeout=0.1)
                except TimeoutError:
                    continue
                self._dispatch(json.loads(raw))
        except Exception as e:
            if not self._stop.is_set():
                self.log(f"web guard stopped: {e!r}")
                self.error = LOST
        finally:
            self.running = False

    def _start(self):
        self._send("Browser.getVersion", cb=self._on_version)
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send("Browser.setDownloadBehavior", {"behavior": "deny"})
        for perm in ("notifications", "geolocation", "midi", "clipboardReadWrite"):
            self._send("Browser.setPermission", {"permission": {"name": perm}, "setting": "denied"})
        self._send("Target.getTargets", cb=self._on_targets)

    def _on_version(self, result):
        """A headless browser says 'HeadlessChrome' in its user agent and client hints, and some sites turn it away.
        Present the same identity the normal browser has."""
        result = result or {}
        ua = result.get("userAgent", "").replace("HeadlessChrome", "Chrome")
        major = (result.get("product", "").split("/")[-1] or "156").split(".")[0]
        full = result.get("product", "").split("/")[-1] or f"{major}.0.0.0"
        brands = [{"brand": "Chromium", "version": major}, {"brand": "Not-A.Brand", "version": "24"},
                  {"brand": "Brave", "version": major}]
        self._ua = (ua, {"brands": brands, "fullVersionList": [{**b, "version": full if b["brand"] != "Not-A.Brand"
                                                                else "24.0.0.0"} for b in brands],
                         "platform": "Windows", "platformVersion": "10.0.0", "architecture": "x86", "model": "",
                         "mobile": False, "bitness": "64"})
        for session in list(self._sessions):
            self._set_identity(session)

    def _set_identity(self, session):
        if self._ua and self._ua[0]:
            self._send("Emulation.setUserAgentOverride", {"userAgent": self._ua[0], "userAgentMetadata": self._ua[1]},
                       session)

    def _apply_view(self):
        """Render at the app's canvas size and stream it (or stop streaming while the tab is hidden)."""
        s = self._main
        phone = self._phone
        if not s or not (self._view or phone):
            return
        if phone:                                         # a phone is browsing: its size, always streaming
            (w, h), on = phone[:2], True
        else:
            w, h, on = self._view
        self._send("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1,
                                                          "mobile": False}, s)
        self._send("Page.stopScreencast", {}, s)
        if on:
            # every frame: a static page produces only one, and skipping frames would never deliver it
            # (frames come out at the CSS size whatever the device scale factor is, so there is no sharper option;
            # a phone's picture gets a higher JPEG quality instead, because it is shown enlarged)
            self._send("Page.startScreencast", {"format": "jpeg", "quality": 82 if phone else 72, "maxWidth": w,
                                                "maxHeight": h, "everyNthFrame": 1}, s)

    def _on_targets(self, result):
        infos = (result or {}).get("targetInfos", [])
        self._baseline = {t["targetId"] for t in infos}      # tabs that existed before we started watching
        self._ready = True
        pages = [t for t in infos if t.get("type") == "page" and not t.get("openerId") and not t.get("subtype")]
        if not pages:
            return
        # The profile persists between runs, so the browser may reopen tabs from last time. Use the one we asked
        # for (or the first) and close the rest: there are no tabs in this window.
        want = (self.home or "").rstrip("/")
        main = next((t for t in pages if want and t.get("url", "").rstrip("/") == want), pages[0])
        for t in pages:
            if t is not main:
                self._send("Target.closeTarget", {"targetId": t["targetId"]})
        self._main_target = main["targetId"]
        self._send("Target.attachToTarget", {"targetId": main["targetId"], "flatten": True},
                   cb=lambda r, tid=main["targetId"]: self._adopt(r["sessionId"], "page", tid, False))
        if want and main.get("url", "").rstrip("/") != want and not main.get("url", "").startswith(want):
            self._cmds.put(("navigate", self.home))

    def _drain(self):
        while True:
            try:
                cmd, arg = self._cmds.get_nowait()
            except queue.Empty:
                return
            s = self._main
            if cmd == "close":
                self._send("Browser.close")
                continue
            if cmd == "allow":
                self._allowed[site_of(host_of(arg))] = time.time() + 60
                cmd, arg = "navigate", arg
            if not s:
                continue
            if cmd == "view":
                self._apply_view()
            elif cmd == "input":
                self._send(arg[0], arg[1], s)
            elif cmd == "navigate":
                self._send("Page.navigate", {"url": arg}, s)
            elif cmd == "youtube":
                self._youtube(arg[0], arg[1], s)
            elif cmd == "reload":
                self._send("Page.reload", {}, s)
            elif cmd == "eval":
                self._send("Runtime.evaluate", {"expression": arg[0], "returnByValue": True}, s, cb=arg[1])
            elif cmd == "history":
                self._send("Page.getNavigationHistory", {}, s, cb=lambda r, d=arg: self._go(r, d))
            elif cmd == "post":
                expr = f"window.postMessage({json.dumps(arg)},'*')"
                for sess in list(self._sessions):
                    self._send("Runtime.evaluate", {"expression": expr}, sess)

    def _go(self, result, delta):
        i = result["currentIndex"] + delta
        entries = result["entries"]
        if 0 <= i < len(entries):
            self._send("Page.navigateToHistoryEntry", {"entryId": entries[i]["id"]}, self._main)

    def _adopt(self, session, typ, target_id, waiting):
        """Set a freshly attached page/iframe target up. Always resumes it, or it would stay frozen."""
        if session in self._sessions:
            return
        self.log(f"attached {typ} session {session[:6]} (waiting={waiting})")
        self._sessions[session] = {"type": typ, "targetId": target_id}
        if typ not in ("page", "iframe"):
            self._send("Runtime.runIfWaitingForDebugger", {}, session)
            return
        if typ == "page" and not self._main:
            self._main = session
        send = lambda m, p=None: self._send(m, p, session)
        send("Page.enable")
        send("Network.enable")
        send("Runtime.enable")
        for name in ("rlGesture", "rlVideo", "rlBlocked", "rlEvent", "rlYoutube", "rlEdit"):
            send("Runtime.addBinding", {"name": name})
        send("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_JS, "runImmediately": True})
        if not waiting:          # already-loaded page; a frame still frozen at start picks the script up on load
            send("Runtime.evaluate", {"expression": PAGE_JS})
        send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        self._set_identity(session)
        if session == self._main:
            self._apply_view()
        send("Runtime.runIfWaitingForDebugger")

    # ---- events -----------------------------------------------------------------
    def _dispatch(self, m):
        if "id" in m:
            cb = self._callbacks.pop(m["id"], None)
            if cb and "result" in m:
                cb(m["result"])
            return
        method, p, session = m.get("method", ""), m.get("params", {}), m.get("sessionId")
        if self.debug and method.startswith("Target."):
            self.log(f"debug {method} {json.dumps(p.get('targetInfo', {}))[:200]}")
        handler = getattr(self, "_on_" + method.replace(".", "_"), None)
        if handler:
            try:
                handler(p, session)
            except Exception as e:
                self.log(f"web guard: {method} failed: {e!r}")

    def _on_Target_attachedToTarget(self, p, session):
        info = p.get("targetInfo", {})
        self._adopt(p["sessionId"], info.get("type", ""), info.get("targetId", ""), p.get("waitingForDebugger"))

    def _on_Target_detachedFromTarget(self, p, session):
        self._sessions.pop(p.get("sessionId"), None)

    def _on_Target_targetCreated(self, p, session):
        info = p.get("targetInfo", {})
        tid = info.get("targetId")
        # The window has no tabs, so any new page we didn't start is a pop-up or pop-under. (Not prerenders, which
        # are hidden pages the browser makes for speed, and not anything that existed before we attached.)
        if (not self._ready or info.get("type") != "page" or info.get("subtype") or tid in self._baseline
                or tid == self._main_target):
            return
        self._baseline.add(tid)
        self.log(f"new window: opener={info.get('openerId')} url={info.get('url', '')[:100]!r}")
        url = info.get("url", "")
        recent = time.time() - self._last_gesture < POPUP_GESTURE
        if url.startswith("http") and recent and self._same_site(url) and self._main:
            self._send("Page.navigate", {"url": url}, self._main)       # a normal link that wanted a new tab
        else:
            self._blocked("popups", "pop-up", url if url.startswith("http") else "")
        self._send("Target.closeTarget", {"targetId": tid})

    def _on_Target_targetInfoChanged(self, p, session):
        info = p.get("targetInfo", {})
        if info.get("targetId") == self._main_target:
            with self._lock:
                self._url, self._title = info.get("url", ""), info.get("title", "")

    def _on_Page_frameNavigated(self, p, session):
        frame = p["frame"]
        if session != self._main or frame.get("parentId"):
            return
        self._main_frame = frame["id"]
        with self._lock:
            key = urllib.parse.urldefrag(frame["url"])[0]
            changed, self._page_key = key != self._page_key, key     # not self._url: targetInfoChanged updates that first
            if changed:                      # a new page: start its tallies and stream list afresh
                self.epoch += 1
                self._cands.clear()
                self._subs.clear()
                self._videos.clear()
                self._unsupported = ""
                self.stats = {"popups": 0, "redirects": 0, "overlays": 0, "ads": 0}
                self.last_block = None
                self._editing = False        # a new page starts with nothing focused
            self._url = frame["url"]

    def _on_Page_navigatedWithinDocument(self, p, session):
        """A site's "next episode" can change the address without loading a page, so frameNavigated never fires. The
        old streams are only set aside, not dropped: sites also rewrite the address for things that keep the video."""
        if session != self._main or (self._main_frame and p.get("frameId") != self._main_frame):
            return
        key = urllib.parse.urldefrag(p.get("url", ""))[0]
        with self._lock:
            if key == self._page_key:
                return
            self._page_key = key
            for c in self._cands.values():
                c.stale = True
            count = len(self._cands)
        self.log(f"page moved to {key[:100]} without loading: {count} earlier streams set aside")

    def _on_Page_frameRequestedNavigation(self, p, session):
        if session != self._main or p.get("frameId") != self._main_frame:
            return
        url = p.get("url", "")
        if p.get("reason") not in ("scriptInitiated", "metaTagRefresh", "httpHeaderRefresh"):
            return
        if not url.startswith("http") or self._same_site(url) or self._is_allowed(url):
            return
        if time.time() - self._last_gesture < GESTURE_WINDOW:
            return                           # you clicked something a moment ago; that's you navigating
        self._send("Page.stopLoading", {}, session)
        self._blocked("redirects", "redirect", url)

    def _on_Page_screencastFrame(self, p, session):
        self._send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, session)   # or it stops sending
        if session == self._main:
            data = base64.b64decode(p["data"])
            with self._lock:
                self._frame_seq, self._frame = self._frame_seq + 1, data
                self.frame_meta = p.get("metadata", {})

    def _on_Page_javascriptDialogOpening(self, p, session):
        # alert()/confirm()/'leave this page?' would freeze a headless page forever. Spam sites use them a lot.
        self._send("Page.handleJavaScriptDialog", {"accept": p.get("type") != "prompt"}, session)

    def _on_Runtime_executionContextCreated(self, p, session):
        ctx = p.get("context", {})
        aux = ctx.get("auxData", {})
        if aux.get("isDefault") and aux.get("frameId"):
            self._ctx_frame[(session, ctx["id"])] = aux["frameId"]

    def _on_Runtime_executionContextsCleared(self, p, session):
        for k in [k for k in self._ctx_frame if k[0] == session]:
            self._ctx_frame.pop(k)

    def _on_Runtime_bindingCalled(self, p, session):
        name = p.get("name")
        if name == "rlGesture":
            self._last_gesture = time.time()
        elif name == "rlBlocked":
            self._blocked("overlays", "click-trap", "")
        elif name == "rlEdit":
            if session == self._main:
                with self._lock:
                    self._editing = str(p.get("payload", "")) == "1"
        elif name == "rlYoutube":
            url = str(p.get("payload", ""))[:2000]
            if url.startswith("https://") and self.on_youtube:
                self.on_youtube(url)
        elif name == "rlEvent":
            try:
                ev = json.loads(p.get("payload", "{}"))
            except ValueError:
                return
            ev["frame"] = self._ctx_frame.get((session, p.get("executionContextId")), "")
            try:
                self.page_events.put_nowait(ev)
            except queue.Full:
                pass                                       # nobody is listening (no cast active); drop it
        elif name == "rlVideo":
            frame = self._ctx_frame.get((session, p.get("executionContextId")), "")
            try:
                items = json.loads(p.get("payload", "[]"))
            except ValueError:
                return
            with self._lock:
                self._videos[(session, frame)] = {"at": time.time(), "items": [{**i, "frame": frame} for i in items]}

    # ---- network capture ------------------------------------------------------------
    def _on_Network_requestWillBeSent(self, p, session):
        if len(self._reqs) > MAX_REQUESTS:
            for k in list(self._reqs)[:MAX_REQUESTS // 3]:
                self._reqs.pop(k, None)
        req = p.get("request", {})
        self._reqs[(session, p["requestId"])] = {"url": req.get("url", ""), "headers": dict(req.get("headers", {})),
                                                 "frame": p.get("frameId", "")}

    def _on_Network_requestWillBeSentExtraInfo(self, p, session):
        r = self._reqs.get((session, p["requestId"]))
        if r:
            r["headers"].update(p.get("headers", {}))        # adds the Cookie header the browser really sent

    def _on_Network_loadingFailed(self, p, session):
        if "BLOCKED_BY_CLIENT" in p.get("errorText", ""):
            with self._lock:
                self.stats["ads"] += 1

    def _on_Network_responseReceived(self, p, session):
        r = self._reqs.get((session, p["requestId"]))
        resp = p.get("response", {})
        if not r or resp.get("status") not in (200, 206):
            return
        url = resp.get("url") or r["url"]
        if not url.startswith(("http://", "https://")):
            return                           # blob: / data: addresses live inside the page; nothing else can fetch them
        if subtitles.is_subtitle_url(url, resp.get("mimeType", "")) and not is_ad_url(url):
            self._add_subtitle(url, r["frame"], replay_headers(r["headers"]))
            return
        kind = classify(url, resp.get("mimeType", ""))
        if kind == "unsupported":
            with self._lock:
                self._unsupported = url
            return
        if is_ad_url(url):
            return
        size = _total_size(resp.get("headers"))
        mime0 = resp.get("mimeType", "").split(";")[0].lower()
        if not kind and p.get("type") in ("XHR", "Fetch", "Media", "Other") and (
                mime0.startswith(("video/", "audio/")) or url.lower().split("?")[0].endswith((".ts", ".m4s", ".aac", ".m4a"))
                or mime0 in ("application/octet-stream", "image/jpeg", "image/png")):
            self._activity[host_of(url)] = time.time()      # a piece of video came from this server just now
        if not kind:
            # Not named like a video. A site hiding its playlist as .txt / .png / "octet-stream" gives itself away
            # by what's inside, so look at small script-made responses of the kinds that get used for disguise.
            mime = resp.get("mimeType", "").split(";")[0].lower()
            if (p.get("type") in ("XHR", "Fetch", "Other") and mime in SNIFF_MIMES and size < 400_000
                    and len(self._sniffing) < 200):
                self._sniffing[(session, p["requestId"])] = (url, r["frame"], replay_headers(r["headers"]), size)
            return
        self._add_candidate(url, kind, r["frame"], replay_headers(r["headers"]), size)

    def _add_subtitle(self, url, frame, headers):
        with self._lock:
            if url not in self._subs and len(self._subs) >= subtitles.MAX_TRACKS * 3:
                self._subs.pop(next(iter(self._subs)))
            self._subs[url] = {"url": url, "frame": frame, "headers": headers}

    def subtitle_sources(self, dur=0):
        """(files, dom_tracks): the subtitle files this page has fetched, and the <track> elements of the video being
        cast (the one whose length is `dur`, if given). subtitles.build_tracks turns these into a list to choose from."""
        now = time.time()
        with self._lock:
            files = [dict(f) for f in self._subs.values()]
            dom = [t for e in self._videos.values() if now - e["at"] < 6 for v in e["items"]
                   if not dur or not v.get("dur") or abs(v["dur"] - dur) < 3 for t in v.get("tracks") or []]
        return files, dom

    def _add_candidate(self, url, kind, frame, headers, size):
        with self._lock:
            cand = self._cands.get(url)
            if cand is None:
                if len(self._cands) >= MAX_CANDIDATES:
                    self._cands.pop(next(iter(self._cands)))
                self._arrivals += 1
                cand = self._cands[url] = Candidate(url=url, kind=kind, frame=frame, seen=self._arrivals)
            cand.headers = headers
            cand.size = max(cand.size, size)
            cand.stale = False

    def _on_Network_loadingFinished(self, p, session):
        item = self._sniffing.pop((session, p["requestId"]), None)
        if not item:
            return
        url, frame, headers, size = item

        def got(result):
            body = result.get("body", "")
            if result.get("base64Encoded"):
                try:
                    body = base64.b64decode(body[:4096 * 2]).decode("utf-8", errors="replace")
                except ValueError:
                    return
            kind = sniff(body)
            if kind:
                self.log(f"found a disguised {kind} playlist at {url[:100]}")
                self._add_candidate(url, kind, frame, headers, size)

        self._send("Network.getResponseBody", {"requestId": p["requestId"]}, session, cb=got)

    # ---- helpers ------------------------------------------------------------------------
    def _same_site(self, url):
        with self._lock:
            page = self._url
        return site_of(host_of(url)) == site_of(host_of(page))

    def _is_allowed(self, url):
        exp = self._allowed.get(site_of(host_of(url)))
        return bool(exp and exp > time.time())

    def _blocked(self, counter, what, url):
        self.log(f"blocked {what}: {url[:120]}")
        with self._lock:
            self.stats[counter] += 1
            if url:
                self.last_block = {"what": what, "url": url}
