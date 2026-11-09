"""Where the TV really is in the video, worked out from cheap position readings that may lag.

A Roku may report its playback position in steps (it updates the number now and then, not continuously), so one
reading can be up to a step behind the picture, and lining the PC up to it leaves the PC that far behind. But readings
bound the truth from both sides: a reading is never ahead of the picture, and when a reading changes, the step happened
after the reading before it. While the TV plays, its position is `time + c` for one constant c, so every reading narrows
c down, and a handful of them pin it to a fraction of a second with no extra requests and no waiting for the next step.
When the TV stops (pause, buffering) the same bounds say where it stopped; when it starts again, about when it did.
Readings that move exactly with the clock are recognised and used as they are.

Pure (times are passed in), so it is tested on its own.
"""

JUMP = 2.5         # a change this far from the time that passed is a seek, a skip or a restart: start afresh
REQ = 0.03         # a reading was taken up to this long before its answer arrived (one request to the TV)
WIDEN = 0.002      # seconds per second the bounds loosen while playing: clocks drift, old readings grow less certain
STILL = 0.04       # a playing reading that hasn't moved after this long means the TV reports in steps
QUICK = 0.25       # readings this close together show whether the TV reports exactly (each one moves with the clock)
SLACK = 0.005      # bounds that cross by more than this disagree: the newest reading wins


def _lower(a, b):
    """min() where None means 'no bound'."""
    return b if a is None else a if b is None else min(a, b)


class TvClock:
    def __init__(self):
        # What has been learned about how this TV reports; kept across reset(), it doesn't change with the video.
        self.grain = None            # the smallest step seen while playing
        self.stepped = False         # a playing reading stood still: the TV reports in steps
        self.exact = 0               # quick readings in a row that moved exactly with the clock
        self.reset()

    def reset(self):
        """Forget where the TV is (a new video, a restart, a different app)."""
        self.last = None             # (state, pos, t) of the newest reading
        self.since = None            # when the TV was first seen in its current state
        self.before = None           # the state it was in before that
        self.lo = self.hi = None     # while playing: bounds on position - time; while stopped: on the position
        self.origin = None           # when the TV was first seen on its current timeline (after a start or a jump)
        self.moves = 0               # forward steps seen on this timeline: a timeline that never moves is a lie

    @property
    def is_exact(self):
        return self.exact >= 3 and not self.stepped

    def _alone(self, play, pos, t):
        """The bounds one reading gives by itself."""
        lo = pos - t if play else pos
        if self.is_exact:
            return lo, lo + REQ
        if self.stepped and self.grain:
            return lo, lo + self.grain + REQ             # a stepped reading lags by less than one step
        return lo, None

    def _learn(self, change, gap):
        if change == 0:
            if gap >= STILL:
                self.stepped, self.exact = True, 0
        elif change > 0:
            self.grain = change if self.grain is None else min(self.grain, change)
            if gap <= QUICK:
                self.exact = self.exact + 1 if abs(change - gap) <= REQ else 0

    def feed(self, state, pos, t):
        """One reading: the TV's state ('play', 'pause', 'buffer', ...) and the position it reported at time t."""
        last = self.last
        if last is None or state != last[0]:
            self.before, self.since = (last[0] if last else None), t
        play = state == "play"
        was = last is not None and last[0] == "play"
        new = None
        if last is not None and self.lo is not None:
            gap = t - last[2]
            if play and was:
                change = pos - last[1]
                if abs(change - gap) <= JUMP:
                    if change > 0 or self.moves:         # (a reading standing still right after a jump may be a lie)
                        self._learn(change, gap)
                    if change > 0:
                        self.moves += 1
                    d = WIDEN * max(0.0, gap)
                    hi = None if self.hi is None else self.hi + d
                    if change > 0:                       # it stepped after the last reading
                        hi = _lower(hi, pos - last[2] + REQ)
                    new = (self.lo - d, hi)
            elif play:
                # Started again at some point since the last reading: it has played for between none and all of that.
                if self.lo - JUMP <= pos <= (self.hi if self.hi is not None else self.lo) + gap + JUMP:
                    new = (self.lo - t, None if self.hi is None else self.hi - last[2] + REQ)
            elif was:
                # Stopped at some point since the last reading was taken.
                at_last, now_hi = self.lo + last[2] - REQ, None if self.hi is None else self.hi + t
                if at_last - JUMP <= pos <= (now_hi if now_hi is not None else at_last + gap) + JUMP:
                    new = (at_last, now_hi)
            elif abs(pos - last[1]) <= JUMP:
                new = (self.lo, self.hi)                 # still stopped where it was
        own = self._alone(play, pos, t)
        if new is None:                                  # the first reading, or the TV jumped: a new timeline
            self.lo, self.hi = own
            self.origin, self.moves = t, 0
        else:
            lo, hi = max(new[0], own[0]), _lower(new[1], own[1])
            if hi is not None and lo > hi + SLACK:       # they disagree (it stalled or skipped unseen): trust this one
                lo, hi = own
            self.lo, self.hi = lo, hi
        self.last = (state, pos, t)

    def bounds(self, t):
        """(earliest, latest) the TV can be at time t; latest is None when nothing bounds it from above yet."""
        if self.last is None or self.lo is None:
            return None, None
        shift = t if self.last[0] == "play" else 0.0
        return self.lo + shift, None if self.hi is None else self.hi + shift

    def now(self, t):
        """The best estimate of the TV's position at time t (None before the first reading)."""
        lo, hi = self.bounds(t)
        if lo is None:
            return None
        if hi is None:
            return lo
        if not self.stepped:                 # until the TV is seen reporting in steps, its readings are taken as exact
            return min(hi, lo + REQ / 2)
        return (lo + hi) / 2

    def width(self):
        """How uncertain the estimate is (seconds between the earliest and the latest the TV can be)."""
        if self.lo is None or self.hi is None:
            return float("inf")
        return self.hi - self.lo

    def playing(self):
        return self.last is not None and self.last[0] == "play"
