"""What the connection health panel shows: whether the TV answers, how often Playlet was reloaded, when the TV buffered,
and, for every stall, which side froze (this app, the browser, or the link between them). No Tk, so it can be tested.

A stall is seen from the app as a long gap between the YouTube page's polls (normally 250 ms apart). `classify` decides
whose fault a gap was by comparing three clocks: the app's own watchdog (did this app freeze?), the page's own timer (did
the browser run its timers late?) and the page's last round trip (did the request itself take long?).
"""
import collections
import threading
import time

WINDOW = 1800.0        # seconds of history kept
STALL_GAP = 1.5        # a gap between page polls longer than this (seconds) is a stall worth naming
APP_LATE = 0.5         # the app's own thread running this late (seconds) means the app froze
EVENTS_SHOWN = 14

APP, CHROME, LINK, UNKNOWN = "app", "browser", "extension link", "unknown"


def suspended_seconds():
    """Seconds this PC has spent asleep or hibernating since it booted, or None if it can't be told (not Windows).

    A watchdog that wakes hours late looks exactly like a frozen app unless sleep is subtracted. Windows keeps two clocks
    that differ by exactly the time asleep: GetTickCount64 counts it, QueryUnbiasedInterruptTime doesn't."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        unbiased = ctypes.c_ulonglong()
        if not k32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
        return max(0.0, k32.GetTickCount64() / 1000.0 - unbiased.value / 1e7)
    except Exception:
        return None


def split_late(late, slept):
    """How much of `late` seconds of lateness the app itself is to blame for, given `slept` seconds of PC sleep in that
    time (None = unknown). Sleep isn't a freeze."""
    if slept is None:
        return late
    return max(0.0, late - slept)


def classify(gap, app_late=0.0, timer_gap=0.0, rtt=0.0):
    """Who stalled when polls from the page went `gap` seconds without arriving.

    app_late:  the longest the app's own watchdog ran late during that time
    timer_gap: the longest gap the page measured between its own timer ticks (0 if it didn't say)
    rtt:       the page's slowest poll round trip (0 if it didn't say)"""
    if gap < STALL_GAP:
        return None
    if app_late >= APP_LATE:
        return APP
    if timer_gap >= STALL_GAP * 0.8:
        return CHROME
    if rtt >= STALL_GAP * 0.8:
        return LINK
    return UNKNOWN


class Health:
    def __init__(self, clock=time.time, log=lambda m: None):
        self.clock, self.log = clock, log
        self._lock = threading.Lock()
        self.events = collections.deque()              # (time, kind, detail)
        self.tv_ok, self.tv_ms, self.tv_seen, self.tv_fails = None, 0.0, 0.0, 0
        self.tv_app, self.tv_state = "", "none"
        self._buffer_since = None
        self._app_stalls = collections.deque()         # (time, seconds late)
        self._last_poll = 0.0
        self.polls = 0

    # ---- recording ------------------------------------------------------------------------------------------------
    def note(self, kind, detail=""):
        now = self.clock()
        with self._lock:
            self.events.append((now, kind, detail))
            self._trim(now)
        self.log(f"health: {kind} {detail}".rstrip())

    def _trim(self, now):
        while self.events and now - self.events[0][0] > WINDOW:
            self.events.popleft()
        while self._app_stalls and now - self._app_stalls[0][0] > WINDOW:
            self._app_stalls.popleft()

    def tv_poll(self, ok, ms=0.0):
        """The app's once-a-second look at the TV: did it answer, and how fast."""
        now, went_down = self.clock(), False
        with self._lock:
            if ok:
                self.tv_ok, self.tv_ms, self.tv_seen, self.tv_fails = True, ms, now, 0
            else:
                self.tv_fails += 1
                if self.tv_fails == 2:                 # one miss is noise; two in a row is the TV not answering
                    self.tv_ok, went_down = False, True
        if went_down:
            self.note("tv-unreachable", "the Roku didn't answer twice in a row")

    def tv_playing(self, state, app):
        """What the TV reports: counts how long it sat buffering."""
        now, ended = self.clock(), None
        with self._lock:
            self.tv_state, self.tv_app = state, app
            if state == "buffer" and self._buffer_since is None:
                self._buffer_since = now
            elif state != "buffer" and self._buffer_since is not None:
                ended, self._buffer_since = now - self._buffer_since, None
        if ended is not None and ended >= 0.5:
            self.note("tv-buffer", f"{ended:.1f}s")

    def app_late(self, seconds):
        """The app's own watchdog (or its window's event loop) ran `seconds` late: this app froze for that long."""
        now = self.clock()
        with self._lock:
            self._app_stalls.append((now, seconds))
        if seconds >= APP_LATE:
            self.note("stall-app", f"{seconds:.1f}s")

    def pc_slept(self, seconds):
        """The PC was asleep or hibernating for `seconds`. Not a stall: nothing froze, the whole machine was off."""
        if seconds >= 5:
            self.note("pc-asleep", f"{seconds:.0f}s")

    def page_poll(self, timer_gap=0.0, rtt=0.0):
        """The YouTube page polled the app. `timer_gap` and `rtt` are what the page measured (seconds, 0 = not given)."""
        now = self.clock()
        with self._lock:
            gap = now - self._last_poll if self._last_poll else 0.0
            self._last_poll = now
            self.polls += 1
            late = max([s for t, s in self._app_stalls if now - t <= max(gap, STALL_GAP) + 0.5] or [0.0])
        who = classify(gap, late, timer_gap, rtt)
        if who and who != APP and gap < 600:           # the app's own freezes were noted by its watchdog; 600 s+ is a closed page
            self.note(f"stall-{who.split()[0]}", f"{gap:.1f}s without a poll (app late {late:.1f}s, page timer {timer_gap:.1f}s, round trip {rtt:.1f}s)")
        return who

    def reload(self, skipped, detail=""):
        self.note("reload-skipped" if skipped else "reload", detail)

    # ---- reading --------------------------------------------------------------------------------------------------
    def snapshot(self):
        now = self.clock()
        with self._lock:
            self._trim(now)
            kinds = collections.Counter(k for _, k, _ in self.events)
            buffered = [d for _, k, d in self.events if k == "tv-buffer"]
            return {
                "tv_ok": self.tv_ok, "tv_ms": self.tv_ms, "tv_seen_ago": (now - self.tv_seen) if self.tv_seen else None,
                "tv_app": self.tv_app, "tv_state": self.tv_state,
                "reloads": kinds["reload"], "reloads_skipped": kinds["reload-skipped"],
                "buffers": kinds["tv-buffer"], "buffer_seconds": sum(float(d.rstrip("s")) for d in buffered),
                "stalls": {"app": kinds["stall-app"], "browser": kinds["stall-browser"], "link": kinds["stall-extension"],
                           "unknown": kinds["stall-unknown"]},
                "tv_down": kinds["tv-unreachable"], "asleep": kinds["pc-asleep"],
                "recent": [(now - t, k, d) for t, k, d in list(self.events)[-EVENTS_SHOWN:]][::-1],
            }

    def report(self, extra=None):
        """Plain text for pasting into a message."""
        s = self.snapshot()
        lines = [f"TV: {'answering' if s['tv_ok'] else 'NOT answering' if s['tv_ok'] is False else 'not checked'}"
                 + (f", {s['tv_ms']:.0f} ms" if s["tv_ok"] else "") + f", app {s['tv_app'] or '-'}, {s['tv_state']}",
                 f"Last 30 min: {s['reloads']} Playlet reloads ({s['reloads_skipped']} avoided), "
                 f"{s['buffers']} TV buffering ({s['buffer_seconds']:.0f}s), {s['tv_down']} TV unreachable",
                 f"Stalls: app {s['stalls']['app']}, browser {s['stalls']['browser']}, extension link {s['stalls']['link']}, "
                 f"unknown {s['stalls']['unknown']}" + (f"  (PC asleep {s['asleep']}x, not counted)" if s["asleep"] else "")]
        lines += [str(x) for x in (extra or [])]
        lines += ["", "Recent:"] + [f"  {ago:5.0f}s ago  {k}  {d}" for ago, k, d in s["recent"]]
        return "\n".join(lines)
