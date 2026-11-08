// Content script for youtube.com, active only inside the Roku Listen app's embedded YouTube window.
// While the app has Auto-cast on, the local player becomes a muted remote: whatever you play, pause, scrub or
// switch to here is mirrored to Playlet on the Roku. Normal Chrome tabs (not an app window) are left alone.
(() => {
  // Can be injected twice (manifest + background on reload). Run once - but a copy left over from before an
  // extension reload is orphaned (its chrome.runtime is dead), so it must not block this one.
  const runtimeAlive = () => { try { return !!chrome.runtime?.id; } catch { return false; } };
  if (window.__rokuListenAlive?.()) return;
  window.__rokuListenAlive = runtimeAlive;

  const isAppWindow = ["standalone", "minimal-ui", "window-controls-overlay"]
    .some((m) => matchMedia(`(display-mode: ${m})`).matches);
  if (!isAppWindow) return;

  let autocast = false, lastReply = 0, castId = null, mutedByUs = null, seekTimer = null;
  let ignoreSeeksUntil = 0;   // set while the app nudges this player onto the TV's timeline
  // How long the app may stay silent before this page decides it was closed and hands YouTube back. It used to be
  // 3 s: a busy PC (a big extraction, a game) can stall the browser or the app for longer than that, and every false
  // "closed" unmuted this player (a second, late sound next to the TV) and re-cast the video from scratch when the
  // app answered again, which rebuffered the TV.
  const APP_SILENT_MS = 15000;
  let polling = 0;            // when the poll in flight was sent (0 = none), so a stall doesn't pile up requests
  let lastTick = Date.now(), worstGap = 0, lastRtt = 0;   // what the app is told so it can say who stalled

  function videoId(href) {
    try {
      const u = new URL(href, location.origin);
      if (u.pathname === "/watch") return u.searchParams.get("v");
      if (u.pathname.startsWith("/shorts/")) return u.pathname.split("/")[2];
    } catch { /* not a URL */ }
    return null;
  }
  const currentId = () => videoId(location.href);
  const title = () => document.title.replace(/^\(\d+\)\s*/, "").replace(/\s*-\s*YouTube$/, "");
  const isAd = () => !!document.querySelector("#movie_player.ad-showing");

  // Only the real player counts: hover previews on the Home feed are separate <video>s and must be ignored.
  function isMainVideo(el) {
    return el instanceof HTMLVideoElement && !!el.closest("#movie_player, #shorts-player") &&
      !el.closest("ytd-video-preview, #inline-preview-player, #video-preview");
  }
  const active = (el) => autocast && isMainVideo(el) && !!currentId() && !isAd();

  function send(event) {
    if (!runtimeAlive()) return;
    try {
      chrome.runtime.sendMessage({ type: "event", event: { ...event, title: title() } }).catch?.(() => {});
    } catch { /* extension reloaded */ }
  }

  function mute(v) {
    if (!v.muted) { v.muted = true; mutedByUs = v; }
  }

  // No playlist is sent: Playlet ignores the video and the time when it is given one, and plays the playlist from its first
  // song. The page moves on to the next song itself and that comes through here as a new video. `resume` says this page
  // only just started following a video that was already playing (Auto-cast came on, the page was reloaded): if the TV
  // is already on that video it keeps its own position and the page follows it, instead of the TV reloading.
  function castNow(v, resume = false) {
    castId = currentId();
    send({ kind: "load", id: castId, t: Math.floor(v.currentTime || 0), resume });
    mute(v);
    toast("▶  Playing on your Roku");
  }

  // ---- player -> TV ----------------------------------------------------------------------------
  document.addEventListener("playing", (e) => {
    const v = e.target;
    if (!active(v)) return;
    if (currentId() !== castId) castNow(v);          // a new video started here: put it on the TV
    else { mute(v); if (Date.now() >= ignoreSeeksUntil) send({ kind: "play", id: castId, t: Math.floor(v.currentTime) }); }
  }, true);

  document.addEventListener("pause", (e) => {
    const v = e.target, id = currentId();
    if (!active(v) || id !== castId || v.ended || Date.now() < ignoreSeeksUntil) return;   // not our own pause from a resync
    // Leaving the watch page also pauses the player; only mirror a pause if we're still on the same video.
    setTimeout(() => { if (currentId() === id && v.paused && !v.seeking) send({ kind: "pause", id }); }, 300);
  }, true);

  // "seeking" carries the target time immediately; "seeked" never fires while paused (YouTube doesn't load the
  // new position until play), so both feed one debounced seek - scrubbing fires many, only the last is sent.
  const onSeek = (e) => {
    const v = e.target;
    if (!active(v) || currentId() !== castId || Date.now() < ignoreSeeksUntil) return;
    clearTimeout(seekTimer);
    seekTimer = setTimeout(() => send({ kind: "seek", id: castId, t: v.currentTime, paused: v.paused }), 150);
  };
  document.addEventListener("seeking", onSeek, true);
  document.addEventListener("seeked", onSeek, true);

  // ---- toast -----------------------------------------------------------------------------------
  let toastEl, toastTimer;
  function toast(text) {
    toastEl ||= document.createElement("div");
    Object.assign(toastEl.style, {
      position: "fixed", left: "50%", bottom: "28px", transform: "translateX(-50%)", zIndex: 2147483647,
      padding: "10px 18px", borderRadius: "12px", font: "500 13px Roboto, system-ui, sans-serif", color: "#fff",
      background: "rgba(15,15,15,.92)", border: "1px solid rgba(255,255,255,.12)",
      boxShadow: "0 8px 28px rgba(0,0,0,.5)", pointerEvents: "none",
    });
    toastEl.textContent = text;
    document.documentElement.appendChild(toastEl);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.remove(), 2200);
  }

  // ---- app link: settings + commands -----------------------------------------------------------
  function setAutocast(on) {
    if (on === autocast) return;
    autocast = on;
    const v = document.querySelector("#movie_player video, #shorts-player video");
    if (on) {
      if (v && !v.paused && currentId() && !isAd()) castNow(v, true);   // turned on mid-video: cast what's playing
    } else {
      castId = null;
      if (mutedByUs) { mutedByUs.muted = false; mutedByUs = null; }
    }
  }

  const mainVideo = () => document.querySelector("#movie_player video, #shorts-player video");

  const timer = setInterval(() => {
    if (!runtimeAlive()) { clearInterval(timer); autocast = false; return; }   // orphaned by a reload: stand down
    // A tick that arrives late (the browser slowed this page's timers, or was too busy) says nothing about whether the
    // app is still there, so it must not count as silence: start the wait again from now.
    const tickNow = Date.now(), late = tickNow - lastTick;
    lastTick = tickNow;
    worstGap = Math.max(worstGap, late);
    if (late > 1500 && lastReply) lastReply = tickNow;
    // Left the video (Home, a channel, search...): the TV goes back to Playlet's home screen too.
    if (autocast && castId && !currentId()) {
      send({ kind: "leave", id: castId });
      castId = null;
    }
    // Each poll carries where this player is, so the app can line it up with the TV.
    const v = mainVideo();
    if (autocast && castId && v && !v.muted) mute(v);   // the sound is on the TV; keep this copy silent
    const state = castId && v ? { id: castId, t: v.currentTime.toFixed(2), paused: v.paused ? 1 : 0, ad: isAd() ? 1 : 0 } : {};
    state.gap = (worstGap / 1000).toFixed(2);           // the page's slowest timer gap and last round trip since the last poll
    state.rtt = (lastRtt / 1000).toFixed(2);
    worstGap = 0;
    if (!polling || Date.now() - polling >= 4000) {   // else the last poll hasn't come back yet: don't queue another
      polling = Date.now();
      try {
        const sentAt = Date.now();
        chrome.runtime.sendMessage({ type: "poll", state }, (cmd) => {
          polling = 0;
          lastRtt = Date.now() - sentAt;
          if (chrome.runtime.lastError || !cmd || !cmd.settings) return;   // app not running
          lastReply = Date.now();
          setAutocast(!!cmd.settings.autocast);
          if (cmd.navigate) location.href = cmd.navigate;
          if (cmd.history) history.go(cmd.history);
          if (cmd.reload) location.reload();
          // The TV is the real screen: snap this (muted) player to its timeline without echoing a seek back.
          // Not during an ad: the ad's clock is not the video's. The app can also match play / pause on a resync.
          if (cmd.sync != null && cmd.syncId === castId && v && !isAd()) {
            ignoreSeeksUntil = Date.now() + 3000;   // YouTube reports a seek late; 1.5 s let some echoes through
            v.currentTime = cmd.sync;
            if (cmd.syncPaused === true && !v.paused) v.pause();
            if (cmd.syncPaused === false && v.paused) v.play().catch(() => {});
          }
        });
      } catch { /* extension reloaded */ }
    }
    if (Date.now() - lastReply > APP_SILENT_MS) setAutocast(false);   // app closed: hand YouTube back to normal
  }, 250);
})();
