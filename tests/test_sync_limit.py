"""The promise: the PC's copy of the video and the TV stay within 1.5 s of each other.

A simulated TV and PC play in virtual time while the real sync code (CastSync for the YouTube tab, WebSync for the Web
tab) watches them, and the things that pulled them apart in real use happen on cue: the TV buffering (it did 77 times
in the log), the PC stalling, SponsorBlock jumping the TV, a nonsense reading while the TV switches, and someone pausing
and resuming with the Roku remote. Run with exact TV readings and with readings that lag in steps, since a Roku may
report its position that way, and again "rough": requests to the TV that take a random time (sometimes a slow 300 ms on
Wi-Fi), a TV that only refreshes the number it reports every so often, and a PC player that applies a move up to a poll
late, stalls while it seeks, and is only seen through a snapshot up to a poll old.
"""
import math
import os
import queue
import random
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roku_listen_gui as g  # noqa: E402
import web_sync  # noqa: E402

LIMIT = 1.5


class Sim:
    """A TV and the PC's muted copy, playing in virtual time."""

    def __init__(self, step=0.0, app="Playlet", rough=None, hold=False):
        self.t = 1000.0
        self.tv, self.tv_state, self.app = 100.0, "play", app
        self.pc, self.pc_paused, self.pc_stalled = 100.0, False, False
        self.step = step          # the TV reports its position in steps this big (0 = exact)
        self.hold = hold          # ...refreshing it every `step` seconds of real time, instead of cutting it into steps
        self.shown, self.shown_at = self.tv, self.t
        self.rng = random.Random(rough) if rough is not None else None
        self.seek_until = 0.0     # the PC's player is seeking (not moving) until then
        self.snap = (self.pc, self.pc_paused, self.t)   # what the PC's last poll said
        self.next_poll = self.t
        self.bogus = 0            # the next N TV readings are nonsense (the TV mid-switch)
        self.events, self.pending, self.trace = [], [], []
        self.t0 = self.t

    # ---- time ---------------------------------------------------------------------------------------------------
    def now(self):
        return self.t

    def sleep(self, d):
        self.advance(d)

    def at(self, dt, fn):
        self.events.append((self.t0 + dt, fn))
        self.events.sort(key=lambda e: e[0])

    def advance(self, d):
        end = self.t + max(0.0, d)
        while self.t < end - 1e-9:
            dt = min(0.01, end - self.t)
            self.t += dt
            if self.tv_state == "play":
                self.tv += dt
                if self.hold and self.t - self.shown_at >= self.step:
                    self.shown, self.shown_at = self.tv, self.t
            if not self.pc_paused and not self.pc_stalled and self.t >= self.seek_until:
                self.pc += dt
            while self.events and self.events[0][0] <= self.t:
                self.events.pop(0)[1]()
            while self.pending and self.pending[0][0] <= self.t:
                target, paused = self.pending.pop(0)[1]
                self.pc = target
                if paused is not None:
                    self.pc_paused = paused
                if self.rng:
                    self.seek_until = self.t + self.rng.uniform(0.05, 0.2)
            if self.t >= self.next_poll:
                self.snap, self.next_poll = (self.pc, self.pc_paused, self.t), self.t + 0.25
            self.trace.append((self.t - self.t0, self.tv - self.pc))

    # ---- the TV over ECP -------------------------------------------------------------------------------------------
    def state(self, host=None):
        took = 0.026
        if self.rng:                              # mostly quick, now and then a slow one on Wi-Fi
            took = self.rng.uniform(0.25, 0.35) if self.rng.random() < 0.05 else self.rng.uniform(0.015, 0.06)
        part = self.rng.uniform(0.2, 0.8) if self.rng else 1.0
        self.advance(took * part)                 # the TV answers somewhere in the middle of the request
        if self.bogus:
            self.bogus -= 1
            pos = 2000.0
        elif self.step and self.hold:
            pos = self.shown
        elif self.step:
            pos = math.floor(self.tv / self.step) * self.step
        else:
            pos = self.tv
        st = {"state": self.tv_state, "app": self.app, "pos": pos, "dur": 3000}
        self.advance(took * (1 - part))
        return st

    def key(self, host_or_name, name=None):
        if (name or host_or_name) == "Play":      # the TV takes a moment to act on its Play key
            self.events.append((self.t + 0.3, lambda: setattr(self, "tv_state",
                                                              "pause" if self.tv_state == "play" else "play")))
            self.events.sort(key=lambda e: e[0])

    # ---- the PC's player: commands reach it on its next poll ---------------------------------------------------
    def move(self, target, paused=None):
        delay = self.rng.uniform(0.02, 0.27) if self.rng else 0.25
        self.pending.append((self.t + delay, (target, paused)))
        self.pending.sort(key=lambda e: e[0])

    def player_t(self):
        """The PC's position as the app sees it: the last poll's, run on to now if it was playing."""
        if not self.rng:
            return self.pc
        t, paused, at = self.snap
        return t if paused else t + (self.t - at)

    def player_paused(self):
        return self.snap[1] if self.rng else self.pc_paused

    # ---- the scenario ---------------------------------------------------------------------------------------------
    def scenario(self):
        self.at(10, lambda: setattr(self, "tv_state", "buffer"))          # Playlet rebuffers for 3 s
        self.at(13, lambda: setattr(self, "tv_state", "play"))
        self.at(40, lambda: setattr(self, "pc_stalled", True))           # the PC's YouTube stalls 2.5 s (a game, say)
        self.at(42.5, lambda: setattr(self, "pc_stalled", False))
        self.at(70, lambda: setattr(self, "tv", self.tv + 60))           # SponsorBlock skips a minute on the TV
        self.at(100, lambda: setattr(self, "bogus", 2))                  # two nonsense readings mid-switch
        self.at(130, lambda: setattr(self, "tv_state", "pause"))         # Roku remote: pause...
        self.at(138, lambda: setattr(self, "tv_state", "play"))          # ...and play again
        self.at(160, lambda: setattr(self, "tv_state", "buffer"))        # a long rebuffer: 6 s
        self.at(166, lambda: setattr(self, "tv_state", "play"))

    def worst(self, start, end):
        return max(abs(d) for t, d in self.trace if start <= t <= end)


STEADY = [(25, 38), (50, 68), (85, 98), (110, 128), (150, 158), (178, 190)]


class Checks:
    def check(self, sim):
        for a, b in STEADY:
            self.assertLessEqual(sim.worst(a, b), 1.0, f"steady {a}-{b}s")
        self.assertLessEqual(sim.worst(9, 20), LIMIT, "TV buffering: the PC must be held, not left running")
        self.assertLessEqual(sim.worst(159, 172), LIMIT, "long TV buffering")
        self.assertLessEqual(sim.worst(129, 145), LIMIT, "paused and resumed with the Roku remote")
        self.assertLessEqual(sim.worst(99, 106), 1.0, "a nonsense reading must not move the PC")
        self.assertLessEqual(sim.worst(42.5 + 3.5, 50), 1.0, "after the PC's own stall it catches up within 3.5 s")
        self.assertLessEqual(sim.worst(74, 80), 1.0, "a jump on the TV is followed within 4 s")


class YouTubeTab(Checks, unittest.TestCase):
    """CastSync (Playlet) keeping the YouTube tab's player beside the TV."""

    def run_sim(self, step, **kw):
        sim = Sim(step, **kw)
        bridge = SimpleNamespace(
            player=lambda: {"id": "abc", "t": sim.player_t(), "paused": sim.player_paused(), "ad": False},
            send=lambda cmd: sim.move(cmd["sync"], cmd.get("syncPaused")))
        with mock.patch.object(g.threading, "Thread"):
            s = g.CastSync(lambda: "h", bridge, None)
        s.current, s.settle_until = "abc", 0.0
        sim.scenario()
        with mock.patch.object(g.time, "time", sim.now), mock.patch.object(g.time, "sleep", sim.sleep), \
                mock.patch.object(g.Remote, "state", sim.state), mock.patch.object(g.Remote, "key", sim.key), \
                mock.patch.object(g.Remote, "playlet_now", lambda h: {"videoid": "abc"}), \
                mock.patch.object(g, "log", lambda m: None):
            while sim.t - sim.t0 < 190:
                s._watch("h")
                sim.sleep(s._wait())              # what the sync thread waits between looks when nothing happens
        return sim

    def test_exact_readings(self):
        self.check(self.run_sim(0.0))

    def test_readings_that_lag_in_half_second_steps(self):
        self.check(self.run_sim(0.5))

    def test_readings_that_lag_in_one_second_steps(self):
        self.check(self.run_sim(1.0))

    def test_rough_conditions(self):
        for step in (0.0, 0.5, 1.0):
            for hold in ((False, True) if step else (False,)):
                for seed in range(4):
                    with self.subTest(step=step, hold=hold, seed=seed):
                        self.check(self.run_sim(step, rough=seed, hold=hold))


class WebTab(Checks, unittest.TestCase):
    """WebSync (RokuCast) keeping the Web tab's page video beside the TV."""

    def run_sim(self, step, **kw):
        sim = Sim(step, app="RokuCast", **kw)
        guard = SimpleNamespace(
            page_events=queue.Queue(), epoch=0, mirror=lambda on: None,
            video_playing=lambda dur=0: not sim.player_paused() and not sim.pc_stalled,
            video_paused=lambda dur=0: sim.player_paused(),
            video_time=lambda dur=0: sim.player_t(),
            seek_page=lambda t, dur, paused=None: sim.move(t, paused))
        tv = SimpleNamespace(state=sim.state, key=sim.key, cast=lambda url: None)
        ws = web_sync.WebSync(guard, tv, relay_add=None)
        cand = SimpleNamespace(kind="hls", duration=3000.0, live=False, url="u", headers={})
        with mock.patch.object(web_sync.threading, "Thread"):
            ws.start(cand, offset=0.0)
        ws.settle_until = 0.0
        sim.scenario()
        with mock.patch.object(web_sync.time, "time", sim.now), mock.patch.object(web_sync.time, "sleep", sim.sleep):
            ws.last_alive = ws.last_mirror = sim.t + 1e9          # leave the page / TV-gone checks out of this
            while sim.t - sim.t0 < 190:
                ws._idle()
                sim.sleep(ws._wait())
        return sim

    def test_exact_readings(self):
        self.check(self.run_sim(0.0))

    def test_readings_that_lag_in_one_second_steps(self):
        self.check(self.run_sim(1.0))

    def test_rough_conditions(self):
        for step in (0.0, 0.5, 1.0):
            for hold in ((False, True) if step else (False,)):
                for seed in range(4):
                    with self.subTest(step=step, hold=hold, seed=seed):
                        self.check(self.run_sim(step, rough=seed, hold=hold))

    def test_a_page_that_cannot_say_paused_still_follows_the_tv(self):
        # Older guards only report "playing", which a stall also turns off: never take a stall for a pause to pass on.
        sim = Sim(1.0, app="RokuCast")
        guard = SimpleNamespace(
            page_events=queue.Queue(), epoch=0, mirror=lambda on: None,
            video_playing=lambda dur=0: not sim.pc_paused and not sim.pc_stalled,
            video_time=lambda dur=0: sim.pc,
            seek_page=lambda t, dur, paused=None: sim.move(t, paused))
        keys = []
        tv = SimpleNamespace(state=sim.state, key=lambda name: (keys.append(name), sim.key(name)), cast=lambda url: None)
        ws = web_sync.WebSync(guard, tv, relay_add=None)
        cand = SimpleNamespace(kind="hls", duration=3000.0, live=False, url="u", headers={})
        with mock.patch.object(web_sync.threading, "Thread"):
            ws.start(cand, offset=0.0)
        ws.settle_until = 0.0
        sim.scenario()
        with mock.patch.object(web_sync.time, "time", sim.now), mock.patch.object(web_sync.time, "sleep", sim.sleep):
            ws.last_alive = ws.last_mirror = sim.t + 1e9
            while sim.t - sim.t0 < 60:
                ws._idle()
                sim.sleep(ws._wait())
        self.assertEqual(keys, [], "the page's stall must not pause the TV")
        self.assertLessEqual(sim.worst(46, 60), 1.0)


if __name__ == "__main__":
    unittest.main()


class WhoChangedLast(unittest.TestCase):
    """When the TV and the PC disagree about playing, whichever changed last says what happened."""

    def youtube(self, seconds, setup):
        sim = Sim(0.0)
        keys = []
        bridge = SimpleNamespace(player=lambda: {"id": "abc", "t": sim.pc, "paused": sim.pc_paused, "ad": False},
                                 send=lambda cmd: sim.move(cmd["sync"], cmd.get("syncPaused")))
        with mock.patch.object(g.threading, "Thread"):
            s = g.CastSync(lambda: "h", bridge, None)
        s.current, s.settle_until = "abc", 0.0
        setup(sim)
        with mock.patch.object(g.time, "time", sim.now), mock.patch.object(g.time, "sleep", sim.sleep), \
                mock.patch.object(g.Remote, "state", sim.state), \
                mock.patch.object(g.Remote, "key", lambda h, name: (keys.append((sim.t - sim.t0, name)), sim.key(name))), \
                mock.patch.object(g.Remote, "playlet_now", lambda h: {"videoid": "abc"}), \
                mock.patch.object(g, "log", lambda m: None):
            while sim.t - sim.t0 < seconds:
                s._watch("h")
                sim.sleep(s._wait())
        return sim, keys

    def test_a_pause_with_the_roku_remote_pauses_the_pc_at_once(self):
        sim, keys = self.youtube(12, lambda sim: sim.at(5, lambda: setattr(sim, "tv_state", "pause")))
        self.assertTrue(sim.pc_paused)
        self.assertEqual(keys, [], "the TV is not touched")
        self.assertLessEqual(sim.worst(5, 12), 1.0)

    def test_a_pause_on_the_pc_whose_event_got_lost_still_reaches_the_tv(self):
        # The page drops its own pause / play for 3 s after the app moves it, so a pause made then never arrives as an
        # event. It used to be undone: the PC was resumed "to match the TV".
        sim, keys = self.youtube(10, lambda sim: sim.at(5, lambda: setattr(sim, "pc_paused", True)))
        self.assertTrue(sim.pc_paused, "the pause made on the PC stands")
        self.assertEqual([k for _, k in keys], ["Play"], "and the TV was paused to match")
        self.assertLessEqual(keys[0][0], 5 + 2.0)
        self.assertEqual(sim.tv_state, "pause")

    def web(self, seconds, setup):
        sim = Sim(0.0, app="RokuCast")
        keys = []
        guard = SimpleNamespace(
            page_events=queue.Queue(), epoch=0, mirror=lambda on: None,
            video_playing=lambda dur=0: not sim.pc_paused and not sim.pc_stalled,
            video_paused=lambda dur=0: sim.pc_paused,
            video_time=lambda dur=0: sim.pc,
            seek_page=lambda t, dur, paused=None: sim.move(t, paused))
        tv = SimpleNamespace(state=sim.state, key=lambda name: (keys.append((sim.t - sim.t0, name)), sim.key(name)),
                             cast=lambda url: None)
        ws = web_sync.WebSync(guard, tv, relay_add=None)
        cand = SimpleNamespace(kind="hls", duration=3000.0, live=False, url="u", headers={})
        with mock.patch.object(web_sync.threading, "Thread"):
            ws.start(cand, offset=0.0)
        ws.settle_until = 0.0
        setup(sim)
        with mock.patch.object(web_sync.time, "time", sim.now), mock.patch.object(web_sync.time, "sleep", sim.sleep):
            ws.last_alive = ws.last_mirror = sim.t + 1e9
            while sim.t - sim.t0 < seconds:
                ws._idle()
                sim.sleep(ws._wait())
        return sim, keys

    def test_web_a_pause_with_the_roku_remote_pauses_the_page_at_once(self):
        sim, keys = self.web(12, lambda sim: sim.at(5, lambda: setattr(sim, "tv_state", "pause")))
        self.assertTrue(sim.pc_paused)
        self.assertEqual(keys, [])
        self.assertLessEqual(sim.worst(5, 12), 1.0)

    def test_web_a_pause_on_the_page_whose_event_got_lost_still_reaches_the_tv(self):
        sim, keys = self.web(10, lambda sim: sim.at(5, lambda: setattr(sim, "pc_paused", True)))
        self.assertTrue(sim.pc_paused)
        self.assertEqual([k for _, k in keys], ["Play"])
        self.assertEqual(sim.tv_state, "pause")
