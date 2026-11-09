"""Keeps the TV following the page's video after a Web cast, like the YouTube tab does for Playlet.

While a cast is active the page's video is a muted remote control: pausing, playing or scrubbing it moves the TV.

* pause / play  -> the Roku's Play key (it only toggles, so it's pressed only when the TV is in the other state)
* scrub, HLS    -> the TV is restarted on the same stream at the new position (the relay serves a playlist that begins
                   there). The RokuCast channel has no seek command, so a seek is a restart.
* scrub, others -> the Roku's Left / Right keys, 10 s each (MP4 and DASH can't be cut at an arbitrary time, unless
                   the cast is upscaled: that's HLS on the TV whatever the source was, so it restarts like HLS)
* drift         -> the muted PC copy is nudged onto the TV's timeline now and then, so the page's slider is honest

Nothing here touches Tk: the owner passes in the guard (page events, mirror and seek commands), a `tv` object with
state() / key(name) / cast(url), and `relay_add(url, headers, kind, start)` returning (public_url, session).
"""
import queue
import threading
import time

from tv_clock import TvClock


class WebSync:
    SETTLE = 3.0      # after a (re)start the TV still reports its previous state for a moment
    DRIFT = 0.4       # seconds of page-vs-TV difference worth correcting (the clock knows the TV to ~0.05 s)
    LIMIT = 1.5       # the promise: the page and the TV are never this far apart for longer than a check or two
    WATCH = 0.5       # seconds between looks at the TV (one ~26 ms request)
    CHECK = 2.0       # seconds between precise drift measurements while both play
    BIG = 15.0        # a drift this large is checked twice before the page moves: a reading taken mid-switch lies
    MISMATCH = 0.5    # seconds the TV may be paused while the page plays (or the other way) before the page follows
    MAX_MOVES = 6     # corrections per minute; beyond this the page can't keep up, so checks slow down
    RECHECK = 20.0    # seconds between drift checks when corrections keep failing to hold
    APPLY = 0.3       # a move reaches the page's video a moment after it is sent
    PC_GRACE = 1.0    # seconds a pause / play made on the page may take to reach the TV before it is passed on here
    SHARP = 0.2       # the TV's position is known to within this (seconds) without taking quick readings
    SEEK_MIN = 2.0    # a jump smaller than this is playback, not a scrub
    MAX_STEPS = 90    # at most 15 minutes of 10 s key presses
    ENDING = 30.0     # the TV stopping this close to the end is the video finishing, not someone leaving it
    LEFT_PAGE = "You left the page, so the TV is no longer following it"
    FINISHED = "That one finished on the TV"
    TV_STOPPED = "The TV stopped playing this"
    BROWSER_GONE = "The browser closed, so the TV is no longer following the page"

    # drift-watch state; class-level so one built without __init__ (tests) still has it
    _watch_at = _check_at = 0.0
    _holding = False             # the page is paused because the TV is buffering
    _mismatch_since = None       # when the TV and the page started disagreeing about pause / play
    _moves = ()                  # times of recent corrections
    _page_seen = None            # (paused, when the page was first seen that way; 0 = not its own change)
    _page_mine_until = 0.0       # the page's pause / play changes until then are this code's own moves

    @property
    def clock(self):
        """Where the TV really is in its stream, from its readings (see tv_clock); add `offset` for the video."""
        c = self.__dict__.get("_clock")
        if c is None:
            c = self.__dict__["_clock"] = TvClock()
        return c

    def _read(self):
        """tv.state(), with every RokuCast reading also teaching the clock."""
        st = self.tv.state()
        if st and st.get("app") in ("RokuCast", ""):
            self.clock.feed(st["state"], st["pos"], time.time())
        return st

    def __init__(self, guard, tv, relay_add, log=lambda m: None, on_stop=lambda reason: None):
        self.guard, self.tv, self.relay_add, self.log, self.on_stop = guard, tv, relay_add, log, on_stop
        self.active = False
        self.lead = 3.0                 # learned: seconds from "start at t" until the TV is really playing
        self.offset = 0.0               # TV position + offset = position in the video
        self.settle_until = 0.0
        self.title = ""
        self.started_at = 0.0
        self._thread = None

    # ---- lifecycle -----------------------------------------------------------
    def start(self, cand, offset, page_time=None, title="", restartable=False):
        """Begin following. `offset` is where the TV's stream started (HLS cut) and page_time where the page was
        when the cast began (used to line up the TV when the stream couldn't be cut). `restartable` says a seek can
        restart the TV at a new position even though the source isn't HLS."""
        self.stop("", quiet=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)             # or it sees active go True again and runs beside the new thread
        self.cand, self.kind, self.dur = cand, cand.kind, float(cand.duration or 0)
        self.restartable = restartable or cand.kind == "hls"
        self.live = bool(cand.live)
        self.offset, self.title = float(offset or 0), title
        self.epoch0 = self.guard.epoch
        self.started_at = time.time()
        self.settle_until = self.started_at + self.SETTLE
        self.tv_seen = 0.0              # the furthest into the video the TV has been seen
        self.clock.reset()
        self._page_seen, self._holding, self._mismatch_since = None, False, None
        self.last_check = self.last_mirror = self.last_alive = time.time()
        self._seek_at = None            # (target, when) of the last seek we acted on
        while True:                     # forget events from before the cast
            try:
                self.guard.page_events.get_nowait()
            except queue.Empty:
                break
        self.active = True
        self.guard.mirror(True)
        self._thread = threading.Thread(target=self._run, args=(page_time,), name="web-sync", daemon=True)
        self._thread.start()

    def stop(self, reason="", quiet=False):
        was = self.active
        self.active = False
        if was:
            try:
                self.guard.mirror(False)             # the page gets its sound back
            except Exception:
                pass
            if not quiet:
                self.on_stop(reason)

    # ---- the loop --------------------------------------------------------------
    def _run(self, page_time):
        try:
            if page_time and page_time > self.SEEK_MIN + 8 and not self.restartable and not self.live:
                self._catch_up(page_time)
            while self.active:
                try:
                    ev = self._next()
                except queue.Empty:
                    self._idle()
                    continue
                self._safe(ev.get("kind"), lambda: self._handle(ev))
        except Exception as e:
            self.log(f"web sync crashed: {e!r}")
            self.stop("The link to the TV broke")

    def _safe(self, what, fn):
        try:
            fn()
        except Exception as e:
            self.log(f"web sync error on {what}: {e!r}")

    FAST = 0.25       # seconds between looks while the TV is stopped: how soon it starts again decides how far apart
    FAST_FOR = 60.0   # ...for this long into a stop; a long pause goes back to WATCH

    def _wait(self):
        """How long the sync thread may sleep between looks at the TV."""
        c = self.clock
        stopped = c.last is not None and c.last[0] in ("pause", "buffer") and c.since is not None
        if self._holding or (stopped and time.time() - c.since < self.FAST_FOR):
            return self.FAST
        return self.WATCH

    def _next(self):
        ev = self.guard.page_events.get(timeout=self._wait())   # idle wake-ups drive the drift watch (WATCH)
        while ev.get("kind") == "seeked":           # scrubbing fires a burst: only the latest matters
            try:
                nxt = self.guard.page_events.get_nowait()
            except queue.Empty:
                break
            if nxt.get("kind") == "seeked":
                ev = nxt
            else:
                self.guard.page_events.queue.appendleft(nxt)
                break
        return ev

    def _idle(self):
        now = time.time()
        if self.guard.epoch != self.epoch0:
            return self.stop(self.LEFT_PAGE)
        if now - self.last_mirror > 3:              # frames that reloaded since need telling again
            self.last_mirror = now
            self.guard.mirror(True)
        if now - self.last_alive > 5 and now > self.settle_until:
            self.last_alive = now
            st = self._read()
            if st is not None and (st["state"] == "none" or st.get("app") not in ("RokuCast", "")):
                ending = self.dur and self.tv_seen >= self.dur - self.ENDING
                return self.stop(self.FINISHED if ending else self.TV_STOPPED)
            if st is not None:
                self.tv_seen = st["pos"] + self.offset
        self._safe("watch", self._watch)

    def _watch(self):
        """Keep the page within LIMIT seconds of the TV between events: look at the TV every WATCH seconds, hold the page
        while the TV buffers (the gap can't be corrected while the TV stands still, only kept from growing), follow the
        TV's pause / play when it changes on its own (Roku remote, phone), and measure the drift precisely every CHECK.
        Used to be one rough look every 15 s. Positions come from the clock, not the raw reading, which can lag a whole
        step. The TV is never moved here to fix the sync: it's the real screen."""
        now = time.time()
        if now < self._watch_at or now < self.settle_until:
            return
        self._watch_at = now + self._wait() - 0.01
        st = self._read()
        if not st or st.get("app") not in ("RokuCast", ""):
            self._holding, self._mismatch_since = False, None
            return
        now = time.time()
        tv_state, tv_pos = st["state"], self.clock.now(now) + self.offset
        playing = self.guard.video_playing(self.dur)
        page_time = self.guard.video_time(self.dur)
        page_paused = self._page_paused(playing)
        if page_time is not None:
            if self._page_seen is None:
                self._page_seen = (page_paused, 0.0)
            elif self._page_seen[0] != page_paused and now >= self._page_mine_until:
                self._page_seen = (page_paused, now)             # paused or played on the page (by you)
        if tv_state == "buffer":                                   # the TV stalled: stop the page where the TV stands
            if playing and not self._holding:
                self._holding = True
                self._move_page(tv_pos, paused=True)
                self.log(f"held the page at {tv_pos:.1f}s while the TV buffers")
            return
        if self._holding:                                          # the TV moves again: let the page go from there
            self._holding = False
            if tv_state == "play":
                self._move_page(tv_pos + self.APPLY, paused=False)
                self.log("let the page go: the TV is playing again")
                self._check_at = time.time() + 1.5
            return
        if tv_state in ("play", "pause") and page_time is not None and (tv_state == "pause") != page_paused:
            self._disagree(tv_state, tv_pos, page_paused, now)
            return
        self._mismatch_since = None
        if tv_state != "play" or not playing:
            return
        # Clearly apart already (the page stalled, the TV jumped): measure now, not at the next check. The clock gives
        # the earliest and latest the TV can be; only a gap beyond both counts as proven.
        if page_time is not None:
            lo, hi = self.clock.bounds(now)
            lo += self.offset
            hi = lo + 1.0 if hi is None else hi + self.offset
            if max(lo - page_time, page_time - hi) > self.DRIFT and now - self.last_check >= 1.0:
                self._check_at = now
        if now >= self._check_at:
            self._check_at = now + self.CHECK
            self._align()

    def _disagree(self, tv_state, tv_pos, page_paused, now):
        """The TV and the page disagree about playing. Whichever changed last says what happened: the TV's own pause or
        play (Roku remote, phone) is followed at once; one made on the page is on its way to the TV as an event, and if
        that got lost (the page drops its own events for 1.5 s after it is moved), it is passed on here."""
        if self._mismatch_since is None:
            self._mismatch_since = now
        waited = now - self._mismatch_since
        tv_at = self.clock.since if self.clock.before is not None else 0.0
        page_at = self._page_seen[1] if self._page_seen else 0.0
        if tv_at > page_at or (tv_at == page_at and waited >= self.MISMATCH):
            self._mismatch_since = None
            paused = tv_state == "pause"
            self._move_page(tv_pos + (0 if paused else self.APPLY), paused=paused)
            self.log(f"page {'paused' if paused else 'resumed'} to match the TV")
            if not paused:
                self._check_at = time.time() + 1.5
        elif page_at > tv_at and waited >= self.PC_GRACE and self.guard.page_events.empty() \
                and getattr(self.guard, "video_paused", None) is not None and not (self.dur and tv_pos > self.dur - 3):
            # (Only with a real paused flag: "not playing" alone can be the page's video stalling, not you pausing it.
            # And not in the last seconds, where the page's copy simply reached the end a moment before the TV.)
            self._mismatch_since = None
            want = "pause" if page_paused else "play"
            self.log(f"the TV missed the page's {want}: passing it on")
            self._ensure(want)
            self._page_seen = (page_paused, 0.0)

    def _page_paused(self, playing):
        """Whether the page's video is paused on purpose (a stall is not a pause). Guards that can't say: not playing."""
        paused = getattr(self.guard, "video_paused", None)
        if paused is not None:
            got = paused(self.dur)
            if got is not None:
                return bool(got)
        return not playing

    def _move_page(self, t, paused=None):
        """Move the page's copy of the video (and play or pause it) without that counting as input."""
        if paused is None:
            self.guard.seek_page(t, self.dur)
            return
        self._page_seen, self._page_mine_until = (bool(paused), 0.0), time.time() + 1.0
        self.guard.seek_page(t, self.dur, paused=paused)

    def _relevant(self, ev):
        """Events from the video we cast, not an ad, a preview thumbnail, or a different video the page swapped in
        (e.g. a "next episode" click) that hasn't reported its own duration yet."""
        if not self.dur:
            return True
        return bool(ev.get("dur")) and abs(ev["dur"] - self.dur) <= 3

    def _handle(self, ev):
        if not self._relevant(ev):
            return
        kind = ev.get("kind")
        self.log(f"web sync {kind} t={ev.get('t'):.1f} paused={ev.get('paused')}" if ev.get("t") is not None else kind)
        if kind == "pause":
            self._ensure("pause")
        elif kind == "play":
            self._ensure("play")
            self._align()
        elif kind == "seeked":
            self._seek(ev["t"], ev.get("paused", False))

    # ---- TV helpers --------------------------------------------------------------
    def _tv_time(self):
        st = self._read()
        if st and st["state"] in ("play", "pause", "buffer") and st.get("app") in ("RokuCast", ""):
            return self.clock.now(time.time()) + self.offset
        return None

    def _ensure(self, want):
        """The Roku Play key toggles, so only press it when the TV is in the other state."""
        time.sleep(max(0.0, self.settle_until - time.time()))
        deadline = time.time() + 8
        while time.time() < deadline and self.active:
            st = self._read()
            state = st and st["state"]
            if state in ("play", "pause"):
                if state != want:
                    self.tv.key("Play")
                    for _ in range(16):            # see the TV flip before the drift watch looks: it would take the
                        st = self._read()          # TV mid-way through our pause for a pause made on the TV itself
                        if st and st["state"] == want:
                            break
                        time.sleep(0.15)
                return
            time.sleep(0.4)

    def _started(self, t0):
        """Wait until the TV is really playing and its position is moving; learn how long that took."""
        last = None
        while time.time() < t0 + 12 and self.active:
            st = self._read()
            if st and st["state"] == "play" and st.get("app") in ("RokuCast", ""):
                if last is not None and st["pos"] > last:
                    took = time.time() - t0 - 0.1
                    self.lead = min(8.0, max(0.5, 0.6 * self.lead + 0.4 * took))
                    self.log(f"TV playing after {took:.2f}s, lead now {self.lead:.2f}s")
                    return True
                last = st["pos"]
            time.sleep(0.25)
        return False

    def _seek(self, target, paused):
        tv_now = self._tv_time()
        if self._seek_at and abs(target - self._seek_at[0]) < 1.0 and time.time() - self._seek_at[1] < 6:
            return                                   # the page re-reporting our own earlier seek
        if tv_now is not None and abs(target - tv_now) < self.SEEK_MIN + (0 if paused else self.lead / 2):
            return                                   # ordinary playback, not a scrub
        self._seek_at = (target, time.time())
        if self.restartable and not self.live:
            aim = max(0.0, target + (0 if paused else self.lead))
            url, sess = self.relay_add(self.cand.url, self.cand.headers, self.kind, aim)
            t0 = time.time()
            self.tv.cast(url)
            self.clock.reset()                       # a new stream: where the old one was says nothing now
            self.settle_until = t0 + self.SETTLE
            end = t0 + 8
            while sess.actual_start is None and time.time() < end and self.active:
                time.sleep(0.2)
            self.offset = sess.actual_start or 0.0   # where in the video the TV's new stream begins
            if paused:
                self._ensure("pause")
            elif self._started(t0):
                self._align()
        elif tv_now is not None and not self.live:
            self._step(target - tv_now + (0 if paused else self.lead))

    def _step(self, delta):
        """Move the TV by about `delta` seconds with its ±10 s keys."""
        steps = max(-self.MAX_STEPS, min(self.MAX_STEPS, round(delta / 10)))
        self.log(f"web sync: {steps:+d} x 10 s")
        for _ in range(abs(steps)):
            if not self.active:
                return
            self.tv.key("Right" if steps > 0 else "Left")
            time.sleep(0.12)
        self.clock.reset()
        self.settle_until = time.time() + 1.0

    def _catch_up(self, page_time):
        """A cast of a stream that can't be cut starts at 0:00: step forward to where the page already was."""
        t0 = time.time()
        end = t0 + 15
        while time.time() < end and self.active:
            st = self._read()
            if st and st["state"] in ("play", "pause") and st.get("app") in ("RokuCast", ""):
                break
            time.sleep(0.4)
        now = self.guard.video_time(self.dur) or page_time
        tv_now = self._tv_time()
        if tv_now is not None and now - tv_now > self.SEEK_MIN + 8:
            self._step(now - tv_now + self.lead)

    def _tv_position(self, near=None):
        """The TV's position in the video right now, or None if it isn't playing. The clock usually knows it already;
        when it is unsure (just after a jump, or before it has learned how this TV reports), quick readings until the
        TV's next step pin it down. Not when it is more than BIG from `near` (the page): that is confirmed by watching."""
        first = self._read()
        if not first or first["state"] != "play" or first.get("app") not in ("RokuCast", ""):
            return None
        start = time.time()
        while self.clock.width() > self.SHARP and time.time() - start < 1.5 and self.active:
            if near is not None and abs(self.clock.now(time.time()) + self.offset - near) > self.BIG:
                break
            time.sleep(0.05)
            st = self._read()
            if not st or st["state"] != "play" or st.get("app") not in ("RokuCast", ""):
                return None
        return self.clock.now(time.time()) + self.offset

    def _jump_holds(self, seconds=1.2):
        """After a big jump, watch the TV for a moment: a real one stays on its new timeline and moves along it; a
        reading taken mid-restart is gone a moment later, or stands still."""
        origin, end = self.clock.origin, time.time() + seconds
        while time.time() < end and self.active:
            time.sleep(0.1)
            st = self._read()
            if not st or st["state"] != "play" or st.get("app") not in ("RokuCast", "") or self.clock.origin != origin:
                return False
        return self.clock.moves > 0

    def _align(self):
        """Snap the muted PC copy onto the TV's timeline if they've drifted apart."""
        self.last_check = time.time()
        if not self.guard.video_playing(self.dur) or time.time() < self.settle_until:
            return
        tv = self._tv_position(near=self.guard.video_time(self.dur))
        page = self.guard.video_time(self.dur)
        if tv is None:
            self._check_at = time.time() + 1.0          # the TV wasn't playing (buffering, starting): look again soon
            return
        if page is None or abs(tv - page) <= self.DRIFT:
            return
        drift = tv - page
        if abs(drift) > self.BIG:
            # One reading this far off is more often the TV mid-restart than a real jump: watch it before moving.
            if not self._jump_holds():
                self.log(f"align skipped: a {drift:+.0f}s reading didn't hold up")
                return
            tv2, page2 = self._tv_position(), self.guard.video_time(self.dur)
            if tv2 is None or page2 is None:
                return
            tv, drift = tv2, tv2 - page2
        now = time.time()
        self._moves = tuple(t for t in self._moves if now - t < 60) + (now,)
        if len(self._moves) > self.MAX_MOVES:
            self._check_at = now + self.RECHECK             # corrections keep failing to hold: ease off, don't fight
            self.log(f"align easing off: {len(self._moves)} corrections in a minute")
        else:
            self._check_at = now + 1.5                      # see that it landed
        self._move_page(tv + self.APPLY)
        self.log(f"aligned the page to the TV (was {drift:+.2f}s off)")
