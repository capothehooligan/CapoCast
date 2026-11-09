"""TvClock: where the TV really is, from readings that may lag in steps."""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tv_clock import TvClock  # noqa: E402


class FakeTv:
    """A TV playing in virtual time that reports its position exactly, or in steps (floor to a multiple of `step`)."""

    def __init__(self, step=0.0, pos=100.0, t=1000.0):
        self.step, self.pos, self.t, self.state = step, pos, t, "play"

    def run(self, dt):
        if self.state == "play":
            self.pos += dt
        self.t += dt

    def reading(self):
        shown = math.floor(self.pos / self.step + 1e-9) * self.step if self.step else self.pos
        return self.state, shown, self.t


def watch(clock, tv, seconds, every=0.526):
    """Read the TV every `every` seconds (what the drift watch does) and return the worst estimate error."""
    worst, end = 0.0, tv.t + seconds
    while tv.t < end:
        tv.run(every)
        clock.feed(*tv.reading())
        worst = max(worst, abs(clock.now(tv.t) - tv.pos))
    return worst


def quick(clock, tv, n=20, every=0.076):
    """Dense readings, like a precise measurement."""
    for _ in range(n):
        tv.run(every)
        clock.feed(*tv.reading())


class Playing(unittest.TestCase):
    def test_nothing_known_before_the_first_reading(self):
        c = TvClock()
        self.assertIsNone(c.now(5.0))
        self.assertEqual(c.width(), float("inf"))

    def test_exact_readings_are_used_as_they_are(self):
        c, tv = TvClock(), FakeTv(step=0.0)
        quick(c, tv)                               # learns that this TV reports exactly
        self.assertTrue(c.is_exact)
        self.assertLess(watch(c, tv, 30), 0.03)
        self.assertLess(c.width(), 0.05)

    def test_readings_in_one_second_steps_are_pinned_far_closer_than_a_step(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        self.assertTrue(c.stepped)
        self.assertAlmostEqual(c.grain, 1.0, places=6)
        # Lining up to the raw reading would leave the PC up to a whole second behind.
        self.assertLess(watch(c, tv, 60), 0.2)

    def test_half_second_steps_read_every_half_second(self):
        # The reading interval is so close to the step that every reading moves: steps only show in quick readings.
        c, tv = TvClock(), FakeTv(step=0.5)
        quick(c, tv)
        self.assertLess(watch(c, tv, 60), 0.15)

    def test_watch_readings_alone_are_never_worse_than_the_raw_reading(self):
        for step in (0.0, 0.5, 1.0):
            c, tv = TvClock(), FakeTv(step=step)
            worst, end = 0.0, tv.t + 40
            while tv.t < end:
                tv.run(0.526)
                st, shown, t = tv.reading()
                c.feed(st, shown, t)
                self.assertGreaterEqual(c.now(t), shown - 1e-9, "never behind what the TV itself says")
                worst = max(worst, abs(c.now(t) - tv.pos))
            self.assertLess(worst, max(step, 0.05) + 0.03, f"step {step}")

    def test_a_skip_starts_afresh_instead_of_averaging(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        watch(c, tv, 10)
        tv.pos += 60.0                             # SponsorBlock skipped a minute
        tv.run(0.5)
        c.feed(*tv.reading())
        self.assertLess(abs(c.now(tv.t) - tv.pos), 1.05)
        self.assertLess(watch(c, tv, 20), 0.25)

    def test_an_unreported_stall_is_noticed_at_the_next_step(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        watch(c, tv, 20)
        tv.t += 0.7                                # stood still 0.7 s without saying so
        self.assertLess(watch(c, tv, 6), 1.05)     # never worse than the raw reading...
        self.assertLess(watch(c, tv, 20), 0.25)    # ...and pinned again soon after

    def test_random_reading_times_stay_close(self):
        rng = random.Random(7)
        for step in (0.0, 0.25, 0.5, 1.0):
            c, tv = TvClock(), FakeTv(step=step)
            quick(c, tv)
            worst = 0.0
            for _ in range(200):
                tv.run(rng.uniform(0.3, 0.9))
                c.feed(*tv.reading())
                worst = max(worst, abs(c.now(tv.t) - tv.pos))
            self.assertLess(worst, 0.2 + step * 0.1, f"step {step}")


class Stopping(unittest.TestCase):
    def test_a_pause_is_placed_better_than_the_stepped_reading(self):
        for step in (0.5, 1.0):
            errs = []
            for phase in range(20):
                c, tv = TvClock(), FakeTv(step=step)
                quick(c, tv)
                watch(c, tv, 10)
                tv.run(phase * 0.026)              # pause at a different point in the step each time
                tv.state = "pause"
                tv.run(0.526 - phase * 0.026)
                c.feed(*tv.reading())
                errs.append(abs(c.now(tv.t) - tv.pos))
            self.assertLess(max(errs), 0.3, f"step {step}: worst {max(errs):.2f}")
            self.assertTrue(all(c.now(tv.t + 5) == c.now(tv.t) for _ in [0]), "a paused TV doesn't move")

    def test_a_paused_exact_reading_is_exact(self):
        c, tv = TvClock(), FakeTv(step=0.0)
        quick(c, tv)
        tv.state = "pause"
        tv.run(0.3)
        c.feed(*tv.reading())
        self.assertAlmostEqual(c.now(tv.t + 3), tv.pos, delta=0.03)

    def test_resuming_is_estimated_from_where_it_stopped(self):
        for step in (0.0, 0.5, 1.0):
            worst = 0.0
            for phase in range(20):
                c, tv = TvClock(), FakeTv(step=step)
                quick(c, tv)
                watch(c, tv, 8)
                tv.state = "pause"
                watch(c, tv, 4)
                tv.run(phase * 0.026)
                tv.state = "play"                  # resumed part-way between two readings
                tv.run(0.526 - phase * 0.026)
                c.feed(*tv.reading())
                worst = max(worst, abs(c.now(tv.t) - tv.pos))
            self.assertLess(worst, 0.6, f"step {step}")

    def test_buffering_then_playing_on(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        watch(c, tv, 8)
        tv.state = "buffer"
        held = watch(c, tv, 6)
        self.assertLess(held, 0.55)
        tv.state = "play"
        self.assertLess(watch(c, tv, 1.2), 0.75)

    def test_a_restart_somewhere_else_is_taken_as_read(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        tv.state = "buffer"
        watch(c, tv, 2)
        tv.pos, tv.state = 400.0, "play"           # Playlet reloaded the video at 6:40
        tv.run(0.5)
        c.feed(*tv.reading())
        self.assertLess(abs(c.now(tv.t) - tv.pos), 1.05)

    def test_state_changes_are_remembered(self):
        c = TvClock()
        c.feed("play", 10.0, 1.0)
        c.feed("play", 10.5, 1.5)
        c.feed("pause", 10.7, 2.0)
        self.assertEqual((c.before, c.since), ("play", 2.0))
        c.feed("pause", 10.7, 2.5)
        self.assertEqual(c.since, 2.0)

    def test_reset_forgets_the_position_but_not_how_the_tv_reports(self):
        c, tv = TvClock(), FakeTv(step=1.0)
        quick(c, tv)
        c.reset()
        self.assertIsNone(c.now(tv.t))
        self.assertTrue(c.stepped)


if __name__ == "__main__":
    unittest.main()
