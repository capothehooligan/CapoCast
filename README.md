<div align="center">

<img src="assets/capocast.png" width="72" alt="">

# CapoCast

**Your Roku's audio on your PC, a YouTube tab that plays everything you pick on your TV, a Web tab that casts videos from any other site with the pop-ups and fake buttons stripped out, and a phone remote that puts it all in your pocket.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)

<img src="docs/screenshots/youtube-tab.png" width="820" alt="CapoCast YouTube tab">

</div>

---

## Why

- **Private listening without your phone.** Roku's "private listening" normally streams TV audio to the Roku mobile app. CapoCast speaks the same protocol, so the sound plays straight from your PC: no phone, no Bluetooth relay, and much lower latency.
- **YouTube on the TV, driven from your PC.** The YouTube tab is your real Chrome, with your login and all your extensions, embedded in the app. Pick a video and it plays on the TV. Pause, scrub, or switch videos, and the TV follows.
- **Your phone as the remote, with no app to install.** Scan a code once and add CapoCast to your iPhone's home screen. It has the Roku remote, a full browser that sends videos to the TV, your bookmarks, and a card showing what's playing that you can scrub. No app store, Apple account or fee.
- **Any other site on the TV, without the junk.** The Web tab is an ad-blocking browser (Brave) built into the app. Browse to anime, obscure streaming sites, anything, press play, then **Cast to TV**. Pop-ups, hijacking redirects and invisible click traps are stopped, and the app sends the real video to the TV, not the ad in front of it.

> [!IMPORTANT]
> **[Playlet](https://github.com/iBicha/playlet) must be installed on your Roku** for the YouTube tab, auto-cast and ±10 s seeking. Playlet is a free, open-source YouTube client for Roku. Install it from the [Roku Channel Store](https://channelstore.roku.com/details/4a41d0921265a5e31429a7679442153f:b5bcb5b630c28b01e93bf59856317b43/playlet), or search for **Playlet** on your Roku, and keep it open on the TV while you use the YouTube tab.

## Why Playlet? A free, ad-free YouTube for your TV

CapoCast plays everything through **Playlet**, a free and open-source YouTube client for Roku. You get what people usually pay a subscription for, plus a few things no official app offers:

- 🔥 **No ads, ever.** No pre-rolls and no unskippable mid-rolls. In the developer's words: *"Not now, not ever."*
- ⚔️ **[SponsorBlock](https://sponsor.ajay.app/) built in.** Automatically skips sponsor reads, intros, "like and subscribe" reminders and other filler, using the community's crowd-sourced segments.
- 🕵️ **No tracking, privacy-first.** Optionally route everything through an [Invidious](https://invidious.io/) backend so you never talk to Google directly.
- 🙋 **Your accounts still work.** Sign in with your YouTube account (or Invidious) to keep your subscriptions and recommendations.
- ⚡ **Fast.** Without the bloat it's quicker than the official Roku app ([see the comparison](https://youtu.be/qmSvA-QQW20)).
- 🔮 **Picture-in-picture** on the TV.
- 📱 **Cast from anywhere:** your phone's YouTube app, a browser, a TV link code, and now your PC through CapoCast.
- 💸 **Free and open source.** No subscription, no paywall, and the code is public ([AGPL-3.0](https://github.com/iBicha/playlet/blob/main/LICENSE)).

| | Official YouTube app | YouTube Premium | **Playlet + CapoCast** |
|---|:---:|:---:|:---:|
| Price | Free | Monthly subscription | **Free** |
| Ads | Yes | None | **None** |
| SponsorBlock (skip sponsors and filler) | ✗ | ✗ | **✓** |
| Privacy (no tracking / Invidious option) | ✗ | ✗ | **✓** |
| Use your YouTube account | ✓ | ✓ | **✓** |
| Open source | ✗ | ✗ | **✓** |
| Browse on your PC with your Chrome extensions, play on the TV | ✗ | ✗ | **✓** |
| Hear the TV through your PC (private listening) | Phone app only | Phone app only | **✓** |

> [!NOTE]
> Playlet is an independent project by [iBicha](https://github.com/iBicha/playlet), and CapoCast isn't affiliated with it. Feature claims above come from [Playlet's own README](https://github.com/iBicha/playlet#features). Like any Roku channel, Roku collects some usage data; Playlet's own telemetry can be turned off in its settings. If you want to support the creators you watch, channel memberships, Patreon and merch go straight to them.

## Features

| | |
|---|---|
| 🎧 **TV audio on your PC** | Roku private listening over Wi-Fi, with a live volume slider (0–200 %) and adjustable buffer delay |
| 🛡️ **Smooth audio over flaky Wi-Fi** | A small sound buffer and gap concealment cover the TV's Wi-Fi pauses, which are about a quarter of a second every few seconds on some TVs. The **Sound delay** slider on the Listen tab sets that buffer (300 ms by default) and moves the sound later or earlier live, so you can drag it until the lips match |
| ⏯️ **Remote control** | Roku-style arrows and OK (hold to repeat), Back, play/pause, rewind, fast-forward, and exact ±10 s seeks, with live "now playing" status |
| ⌨️ **Type on TV** | The keyboard icon at the top right shows a box for typing into the TV's search and sign-in boxes from your PC keyboard (Ctrl+V pastes); arrows, Enter and Esc work the menus. Like the phone app, it pops up by itself when a keyboard opens on the TV |
| ▶️ **Embedded YouTube** | Your own Chrome profile (login, extensions, dark mode) inside the app window |
| 📺 **Auto-cast sync** | Play, pause, scrub, autoplay-next and Shorts mirror to the TV; going back to Home sends Playlet back to its home screen |
| 🎯 **Latency compensation** | Learns how long Playlet takes to rebuffer and aims seeks ahead, then keeps the PC player locked to the TV's timeline |
| 🔁 **Resync** | One button (top bar of the YouTube tab, and on the phone) that lines the video on your PC up with the one on the TV again, whichever way they drifted: position, play / pause, and a TV that moved on or lost the video |
| 🧩 **Playlet Cast extension** | One-click "cast this tab" from normal Chrome too (Alt+Shift+C, or right-click a link → *Cast link to Playlet*) |
| 🔊 **Volume that follows the sound** | One slider that controls the PC volume (0–200 %) while audio is routed to this PC, and the TV's own volume otherwise, switching by itself and following the TV if you use its remote |
| 🌐 **Web tab** | Browse any site in a private, windowless Brave (the app draws the page itself), then cast the video on the page to the TV (HLS, DASH and MP4). Pause, play and scrubbing on the page follow on the TV |
| 🛡️ **Pop-up and trap protection** | Closes pop-ups and pop-unders, stops scripted cross-site redirects, removes invisible click-trap layers, denies downloads and notification prompts. Brave's Shields block the ad requests themselves |
| ✨ **4K upscaling for the TV** | Your PC sharpens Web tab casts to 4K as they play, with Anime4K for anime or FSRCNNX for live action, and the TV just plays the result (needs an NVIDIA GPU) |
| 🚀 **Quick apps** | Tiles under the remote that open an app on the TV in one click. Playlet is there from the start; **All apps** lists everything on your Roku, and you can pin up to five |
| 🔈 **Choose where the sound goes** | An **Output** menu on the Listen tab picks the PC's sound device, and it switches while connected. If the device disappears, CapoCast keeps trying for 30 s before giving up |
| 📱 **Phone remote** | Control everything from your phone: an iPhone home-screen web app (free, no app store or signing) with the d-pad, playback, volume, typing on the TV, the Roku's apps, and your YouTube feed and Playlet settings. Scan the QR code once; it works from then on |
| 🧭 **Phone browser** | Browse the Web tab from your phone (tap, scroll, type, address bar) and press **Cast this page**. Bookmarks and history live on your PC, a "Watching" banner follows what the PC is casting, and play/pause and a scrub bar keep the page, the TV and Discord together |
| 🔗 **Share-menu cast** | One-time iOS Shortcut so **Cast to TV** appears in the Share menu of Safari, YouTube and most apps. YouTube links go to Playlet, other sites cast through the Web tab |
| 🌈 **Simulated HDR for the TV** | Turn on **HDR** in the Web tab and your PC lifts the highlights and sends the TV a 10-bit HDR10 stream (needs an NVIDIA GPU and an HDR10 TV) |
| 💬 **Subtitles on the TV** | Pick a subtitle language in the Web tab and the PC burns it into the upscaled cast (needs an NVIDIA GPU) |
| ⏭️ **Continue Watching** | The Web tab remembers where the TV stopped in each show. A **Continue** menu on the PC and a **Continue** page in the phone's Pages sheet resume it, play the next episode, or start over |
| 🩺 **Connection health** | A panel (the pulse icon at the top right) showing whether the TV answers, Playlet reloads, TV buffering, and, for every stall, whether this app, the browser or the link froze. **Copy report** puts it on the clipboard |
| 🎮 **Discord status** | Optional: shows what you're watching as your Discord status, with a thumbnail and a clock that follows the TV's real position |
| 🎭 **Decoy filter** | Ignores ad clips, previews, thumbnails and loose segments, and picks the long video from the frame that's actually playing. If two streams look alike, it asks you |

<img src="docs/screenshots/listen-tab.png" width="640" alt="CapoCast Listen tab">

## Prerequisites

- **Windows 10 or 11**
- **A Roku TV or player that supports private listening.** If the official Roku mobile app can do private listening on your device, CapoCast can too.
- **[Playlet](https://github.com/iBicha/playlet) installed on the Roku**, for YouTube and auto-cast. See above.
- **Google Chrome**, signed in to YouTube if you want your own feed.
- *For the Web tab:* **[Brave](https://brave.com/download/)** (`winget install Brave.Brave`), opened once so it can download its ad filters, and the **RokuCast channel** sideloaded on your Roku (Roku developer mode). The Web tab plays video through that channel.
- **[FFmpeg](https://ffmpeg.org/download.html) on your `PATH`.** It decodes the audio stream. The easiest install is `winget install Gyan.FFmpeg`.
- *For upscaling (optional):* an **NVIDIA GPU**, a **4K TV**, and the full FFmpeg build above.
- The PC and the Roku on the **same network**.
- *Only when running from source:* Python 3.11 or newer.

## Installation

### Option A: download (recommended)

1. Grab **`CapoCast.exe`** and **`playlet-cast-extension.zip`** from the [latest release](../../releases/latest).
2. Unzip the extension somewhere permanent, for example `Documents\playlet-cast`.
3. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select that folder.
4. Open the extension's **Options** and enter your Roku's IP address. You can find it on the Roku under *Settings → Network → About*.
5. Run `CapoCast.exe`. If Windows Firewall asks, allow it on **private networks**; the audio arrives over UDP.

### Option B: from source

```powershell
git clone https://github.com/6wen66/roku-listen.git
cd roku-listen
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python roku_listen_gui.py
```

Then load the `extension/` folder in Chrome as in steps 3–4 above. To build your own `.exe`, run `.\build.ps1 -Clean`.

## Usage

### Listen tab

1. Enter your Roku's IP (it's saved for next time) and click **Connect**. The TV's speakers mute and the audio plays on your PC.
2. **Volume** follows wherever the sound is going, and switches by itself. While you're connected (the sound plays on this PC) it's the PC volume, **0–200 %**, where everything above 100 % is a boost. While you're not connected (the sound plays on the TV) it's the **TV's own volume**, 0–100. Dragging it steps the TV up or down, it follows the TV live if you use the physical remote, and a **Mute** switch appears. The Roku can't be asked for its volume, so it shows **—** until the TV first reports one, which happens on your first change.
3. **Sound delay** (the slider under Volume) moves the sound later or earlier, live: drag it while you watch until the lips match. It's also the cushion that covers the TV's Wi-Fi pauses, so very low values can skip. **Roku buffer** (in ms) sets the Roku-side buffer on top of that (changes when you reconnect). Lower it (about 200) for tighter lip-sync, or raise it if you hear crackles. **Output** picks which sound device plays the TV's audio and applies at once while connected; "System default" follows Windows. **Find**, beside the Roku IP, looks for Rokus on your network so you don't have to type the address. If the sound stops arriving, the status changes to **No audio from the Roku** and goes back when it returns.
4. **Apps** (under the remote) are one-click tiles that open an app on the TV. Playlet is there to start with. **All apps** lists everything on your Roku, where you can open an app or pin up to five to the tiles; right-click a tile to unpin it.
5. The thin bar under the "now playing" line shows how far into the video the TV is; click it to jump there (YouTube in Playlet). The transport buttons control whatever is playing on the Roku. The first one is the remote's **Back** button. The **Remote** column beside them has the remote's arrows and **OK** as square buttons. Holding an arrow repeats it.
6. **Type on TV:** click the keyboard icon at the top right (next to the pulse icon) to show the box, then type. Letters go into whatever search or sign-in box is open on the TV, and **Ctrl+V** pastes, which is handy for long passwords. While the box is selected, the arrow keys move around the TV's menus, **Enter** is OK, **Esc** is Back and **Backspace** deletes. Click anywhere else in the app to stop sending keys, or click the icon again to hide the box.
7. **Keyboard pop-up:** when a search or sign-in keyboard opens on the TV, the app switches to the Listen tab, shows the Type box if it was hidden, selects it (outlined in green) and comes to the front. If another app such as a game is in the foreground, Windows flashes the taskbar button instead. The box shows the TV's own text, with passwords as dots. When the TV's keyboard closes, the box hides again (if it opened by itself) and the app goes back to the tab you were on. While the box is showing you can start typing anywhere on the Listen tab without clicking it first.

### YouTube tab

1. Open Playlet on the TV.
2. Switch to the **YouTube** tab. Chrome opens inside the app.
3. With **Auto-cast** on (top-right switch):

| You do this on the PC | The TV does this |
|---|---|
| Click any video (or a hover preview, autoplay, a Short) | Plays it through Playlet. The PC copy is muted. |
| Pause / play | Pauses / plays |
| Scrub the timeline | Seeks, aimed ahead to cover Playlet's rebuffer |
| Go back to Home, search or a channel | Closes the player and returns to Playlet's home screen |

The status in the top bar shows the link state: **Ready**, **On TV · \<title\>**, **Connecting…**, or a hint if something's wrong. Turning Auto-cast off unmutes the PC and leaves the TV alone.

**Resync.** If the video on the PC and the one on the TV drift apart (a stall, a Wi-Fi pause, something paused from the remote), click **Resync** in the top bar. The TV is the reference: the PC player is moved onto its position, measured again and corrected until they agree, and its play / pause is matched. If the TV moved on to another video (autoplay, or someone used the remote) the PC opens that one; if the TV lost the video, the PC's video is sent to it again. During an ad on the PC it asks you to wait. The result appears in the status for a few seconds. The same button is on the phone's now-playing card. It needs Auto-cast on, and the extension reloaded after this update (version 1.2.4) to also match play / pause and to skip ads.

**Playlists.** A YouTube playlist no longer restarts from its first song when you click another one. The TV is sent the one video you picked (Playlet ignores the video and the time when it is given a playlist), the page moves on to the next song itself, and each of those is sent like any other video. Before a different video is sent, Playlet's queue is emptied: it never forgets what it has played, and after a few hours it held thousands of entries that slowed every request. Seeking inside a video keeps the queue. This also needs the extension reloaded (version 1.2.4).

### Web tab

<img src="docs/screenshots/web-tab.png" width="820" alt="CapoCast Web tab on the repo's hostile test page: the status line shows a click trap removed and the video streams found">

*The Web tab on the repo's own test page (`tests/decoy_site.py`), which is full of traps. The status line shows what was stopped.*

1. Switch to the **Web** tab. The first time, CapoCast starts its own private Brave. Brave has no window of its own: the app draws the page right in the tab and you click, scroll and type in it as normal, so nothing opens on your desktop or takes focus from a game. It has its own profile, so your normal browser and the YouTube tab are untouched, and you sign in to sites separately here.
2. Go to any site and press play on the video. **Set as home** (right of the status line) makes the page you're on the start page the Web tab opens with every time, and **Home** takes you back to it. The address box takes URLs or searches. The line under it counts what was blocked and how many video streams were found.
3. Click **Cast to TV**. The app picks the real video and starts it on the TV in the RokuCast channel, from where the page is now. While it works the button becomes **Cancel**, so you can back out at any point. From then on the page's video is a muted remote for the TV, like the YouTube tab:

| You do this on the page | The TV does this |
|---|---|
| Pause / play | Pauses / plays |
| Drag the timeline | Jumps there (an HLS stream restarts at the new spot in a couple of seconds; MP4 and DASH step with the Roku's ±10 s keys, up to 15 minutes at a time) |
| Click **Stop casting** (next to the status line) | Leaves the channel, and the page gets its sound back |
| Move to the next episode, or the site plays it for you | Follows on its own: an **Up next** warning shows for 5 seconds, with **Cancel** if you'd rather stay put |
| Go to another page | The TV keeps playing but stops following, and the page gets its sound back |

   The page is muted while the TV follows it. Before you cast, and after you stop, it plays with sound through this PC like any browser. The Listen tab's remote buttons work as well.

   The next episode only follows when the page's address looks like the same show (`?s=1&e=5` after `?s=1&e=4`, or `/naruto-episode-5`) and a new full-length video is actually playing, so a trailer or a different movie won't take over the TV. If a site's addresses don't fit that pattern, just click **Cast to TV** on the new episode.

| The page tries to… | CapoCast… |
|---|---|
| Show ads and trackers | Brave's Shields block the requests |
| Open a pop-up or pop-under | Closes it. A normal same-site link that wants a new tab opens in the Web view instead |
| Redirect you to another site with no click from you | Stops it and shows **Blocked site · Allow** so you can let it through |
| Lay an invisible layer over the page to steal your first click | Removes the layer |
| Start a download or ask for notifications | Denies it |
| Show an ad video before the real one | Ignores clips under 90 s and anything from ad hosts, then picks the stream from the frame that's actually playing |

If the page offers two equally likely streams, a small picker lists them with their length and resolution.

Things to know:

- **Your PC relays the stream.** Many sites only serve a video to the page that asked for it (they check the Referer or cookies), so the Roku fetches it from this PC, and the PC fetches it from the site with the browser's headers. Keep the PC on while it plays. The relay listens on your LAN address on TCP 8766–8770, answers only your Roku, and only for streams you cast.
- **DRM is refused.** Netflix, Crunchyroll, Disney+ and similar can't be cast, and the app won't try to get around that. Use their own Roku channels.
- **HLS, DASH and MP4.** WebM and MKV streams get a clear message instead of a black screen. Sites that hide their playlist under another name (`list.txt`, segments called `.jpg`) are recognised by what's inside. If the browser sees no stream at all, Cast asks [yt-dlp](https://github.com/yt-dlp/yt-dlp), if it's installed.
- **The page is a picture.** Brave streams screenshots of the page to the app, so scrolling is a little less smooth than a real browser, and things that need a real window (drop-down lists, right-click menus, file pickers, full-screen) don't appear. Sites' own JavaScript alerts are dismissed automatically.
- **Subtitles** that are burned into the video just show. Separate subtitle files (WebVTT, SRT, ASS) can be added by your PC: see [Subtitles](#subtitles-on-the-tv) below.
- **Auto-cast switches itself off** while a Web cast is playing, so the YouTube tab doesn't take the TV back. Turn it on again when you want it.
- **Scrubbing isn't instant.** The RokuCast channel has no "seek to" command, so an HLS scrub restarts the stream at the new position (a couple of seconds) and an MP4 or DASH scrub steps ±10 s at a time. The muted copy on your PC is nudged back onto the TV's timeline now and then, so the page's slider stays honest.
- **It checks the stream has picture and sound** before casting (a site may serve video and audio as separate playlists), treats the same video offered several times as one, and starts each cast on a fresh channel. If a stream starts failing part-way, the status line says so.
- **Ads baked into the video itself** can't be removed.

#### Subtitles on the TV

The RokuCast channel on the TV can't show a separate subtitle file, so your PC draws the words onto the video itself and the TV plays the result, the same way upscaling works.

**Subtitles** (right of the status line) steps through **Off** and each language the page has offered, and the language is remembered for other pages. It applies from the next cast.

1. Open the video and turn **captions on in the site's own player** (many players only load a subtitle file once you ask for it).
2. Click **Subtitles** until it shows the language you want. If the page has offered none yet, the status line says so.
3. Press **Cast to TV**. The status line shows "Getting the English subtitles…" and then the cast starts.

What it picks up: subtitle files the player fetched, its `<track>` elements, and the tracks listed in the stream's own playlist. Plain WebVTT and SRT get a bold white font with a dark outline, sized for a TV across the room. A fan-made `.ass` file keeps its own styling. If the page has no track in the language you chose, or the file can't be fetched, the cast carries on without subtitles and says why.

- **It needs FFmpeg with subtitle support and an NVIDIA GPU** (the full build, `winget install Gyan.FFmpeg`), the same as upscaling. With **Upscale** off it only draws the words, at the video's own size, which costs much less than upscaling. With Upscale on, the words are drawn after the upscale so they stay sharp.
- **Seeking keeps them in time.** A scrub restarts the encoder at the new position, and the words stay lined up with the picture.
- **They're part of the picture**, so they can't be turned off from the TV. Click **Subtitles** back to **Off** and cast again.
- **Not for live streams or videos of unknown length**, which cast as they are.

#### Upscaling for the TV

**Upscale** (right of the status line) sends the TV a 4K copy made on your PC. Click it to step through the settings; it applies from the next cast and is remembered.

| Setting | For | What it does |
|---|---|---|
| **Off** | Anything | Casts the video as it is, and the TV does its own scaling |
| **Anime4K** | Anime and cartoons | [Anime4K](https://github.com/bloc97/Anime4K) sharpens line art. Lighter than HQ |
| **Anime4K HQ** | Anime and cartoons | The same with larger networks |
| **Movie** | Live action | [FSRCNNX](https://github.com/igv/FSRCNN-TensorFlow) keeps fine detail such as skin and film grain. The gain is subtle, so it's most useful on 720p and 1080p sources |

A Roku can't run shaders, so your PC does the work while you watch, and the TV plays the result. That means:

- **It needs an NVIDIA GPU, a 4K TV, and the full FFmpeg build** (`winget install Gyan.FFmpeg`).
- **It shares your GPU.** Casting this way during a game costs the game some frames, and a busy GPU can make the TV buffer. If upscaling runs slower than the video, the status line says so.
- **The cast takes a few seconds longer to start**, and so does each scrub.
- **Match the setting to the video.** Anime4K sharpens line art, and on live action it can look painted, so use **Movie** there.
- **It uses more of your network:** up to 16 Mbit/s between the PC and the TV. If the TV buffers on Wi-Fi with the GPU idle, the network is the likely cause.
- **Web tab only.** The YouTube tab plays in Playlet straight from YouTube, so it never passes through the PC.

Live streams, videos already in 4K (or close: a widescreen film that is nearly 3840 wide gains nothing from a shader, so it isn't re-encoded) and videos of unknown length are cast as they are, and if the upscaler can't start the cast carries on without it. Either way the status line tells you.

#### HDR for the TV

**HDR** (next to Upscale) is off until you turn it on. It gives ordinary (SDR) video a simulated HDR picture: your PC keeps the dark and middle tones as they were, lifts the brightest highlights to about 500 nits, and sends the TV a 10-bit HDR10 stream, so the TV switches to its HDR mode. It works on its own or together with Upscale and Subtitles, and it's remembered.

- **It's simulated.** The video has no real HDR information, so this stretches the highlights rather than recovering detail. Some films look better for it and some don't; turn it off if the picture looks harsh. How bright it gets depends on your TV, and the peak is `HDR_PEAK` in `upscaler.py`.
- **It needs an NVIDIA GPU and the full FFmpeg build**, like Upscale, and your TV has to support HDR10. Every cast with HDR on goes through the PC, so it takes a few seconds longer to start, and so does each scrub.
- **The TV may flash black** when it changes into HDR mode.
- **Subtitles are dimmed a little** so white text isn't blindingly bright once the highlights are lifted.
- **Real HDR videos, live streams and videos of unknown length** don't get it (the video is cast without the HDR step), and the status line says so.

#### Discord status

**Discord** shows what's on your TV as your Discord status: the title, a thumbnail of what you're watching, a clock that follows the video, and whether it's being upscaled or has subtitles. There are two switches, both **off** until you turn them on:

- **Web tab:** **Discord** (right of the status line). The first line is what you're watching, taken from the page's title with the site's name and filler like "Watch … English Sub Online" removed. The second line names the site ("on Miruro", plus the upscaling if it's on). The picture is the page's own preview image (or the video's poster); a site's logo is skipped in favour of the poster. Not every site has one.
- **YouTube tab:** **Discord**, beside Auto-cast in the top bar. It shows the video's title and thumbnail. It also covers YouTube videos started from your phone (the phone's YouTube tab, or a YouTube link you send): CapoCast asks Playlet what's playing every few seconds, so it doesn't matter where the video came from.

**The clock follows what you watch**, not how long ago you pressed Cast. CapoCast reads the TV's real position once a second: when the video's length is known Discord counts down the time left ("12:03 left"), and when it isn't (a live stream) it counts up. Pausing stops the clock and shows "Paused", and resuming, scrubbing or a long buffer put it right again. Discord is only told when something actually changes.

Either way, your status shows the title to anyone who can see it, and the most recent thing you started is what's shown.

Discord only shows a status for an app you've registered, so the first time you click it CapoCast asks for an Application ID:

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and press **New Application**.
2. Name it what you want to see after "Playing" (for example `CapoCast`).
3. Copy the **Application ID** from the General Information page and paste it into CapoCast.

The text is sent only to the Discord app on your PC, over its local connection. The thumbnail is sent as a web address, so Discord (and the people who see your status) fetch that picture from the site or YouTube themselves. If Discord refuses the picture, the text is shown without it. The status clears when the cast ends, the TV leaves it, or you turn the switch off, and it needs the Discord desktop app to be open.

### Phone remote

Control the TV from an iPhone (or any phone) on the same Wi-Fi, with no app store, no Apple account and no signing. Click the phone icon at the top right, turn it on, scan the code with the phone's camera, open the link in Safari, then **Share > Add to Home Screen**. It then opens full screen like an app.

- **Remote:** d-pad (hold to repeat), play/pause, skip 10 s, back, home, volume and mute, and a box for typing on the TV.
- **Browse:** the Web tab's browser, on your phone. It shows the PC's page live (the same ad blocking and pop-up protection) and you tap, drag to scroll, type, and use the address bar, back and forward like any phone browser. **Cast this page** sends whatever you're looking at to the TV, with the same video finding as the PC's Cast button. Its play/pause and 10-second buttons control the page's video, so the TV, the page and Discord stay together. The picture is the PC browser's, so it is a little soft on a high-resolution screen.
- **Now playing:** at the top of the Remote tab, a card shows what the TV is playing (a Web cast's page title, a YouTube video's title, or the app's name) with a bar you can drag to jump to a time. For Web casts the page's video moves and the TV follows; for YouTube, Playlet is sent the exact time. Other apps show their position but only have the skip buttons.
- **Bookmarks and history:** in Browse, ☆ saves the page and ☰ opens your bookmarks and recent pages. They are kept on the PC (`%APPDATA%\RokuListen.phone.json`), so they are the same on every phone and survive reinstalling the home-screen app. History records the pages the Web tab visits (also from the PC) and has a Clear button.
- **Cast from the Share menu:** on the Cast tab, "make Cast to TV appear in the Share menu" opens step-by-step instructions for an iOS Shortcut that sends the page you're sharing to the TV, from Safari, YouTube and most apps. (iOS doesn't let a home-screen web app appear in the Share menu by itself, so the one-time Shortcut is the way.)
- **Follows your PC:** while the TV is following a page, every tab shows a "Watching" banner. When a cast starts on the PC (or you open the phone app mid-cast) the phone jumps to Browse on that page; the **Auto-open** switch in the banner turns this off. While casting, the Remote tab's play/pause and skip buttons also go through the page, so everything stays in sync.
- **Cast:** paste a link. YouTube links go straight to Playlet on the TV; any other site is opened in the Web tab's ad-blocking browser on the PC and cast from there (so CapoCast must be running, and the video has to start by itself).
- **Apps:** the Roku's apps, with their icons.
- **YouTube:** the real youtube.com, drawn black (true black, for an OLED screen), so you get YouTube's own recommendations and algorithm. It is the Web tab's ad-blocking browser on the PC, shown on your phone like the Browse tab. Tap **Sign in** once and type your Google account on the phone; the browser remembers it, so your feed, subscriptions and history are there from then on. **Tapping a video plays it on the TV** through Playlet (ad-free, in 4K where the video has it) and leaves you on the feed, so nothing plays on the PC. The ⚙ button opens Playlet's own settings page, served by the Roku while Playlet is open on the TV (the bar says which Playlet account is active; that is the TV's profile, so switching it there switches it on the TV too).

Anyone holding the link can browse as your PC's Web tab (it is the same browser and profile), so treat the link like a password: once you sign in to Google in the YouTube tab, that browser is signed in, and anyone with the link can use it. It is off until you turn it on. Only devices on a private network can reach it, every request needs the secret in the link (use **Make a new link** to cut off old phones), and the phone can't send power keys. Windows may ask once to allow CapoCast on private networks. The link is plain HTTP on your home network, like the Roku's own remote protocol.

### Command-line client

For a minimal, audio-only setup without the GUI:

```powershell
python roku_listen.py --host 192.168.1.50 --delay-ms 300
```

## How it works

```
 ┌───────────── CapoCast (Python / Tk) ──────────────┐        ┌────────── Roku ──────────┐
 │  Listen tab ── ECP WebSocket :8060 (auth + setup) ───┼───────▶│  private listening       │
 │      ▲         RTP/Opus audio ◀── UDP :6970 ─────────┼────────│  (audio stream)          │
 │      └ ffmpeg → PCM → volume → speakers              │        │                          │
 │                                                      │        │                          │
 │  YouTube tab ── embedded Chrome (your profile)       │        │                          │
 │      └ Playlet Cast extension (feed.js)              │        │                          │
 │            │ play / pause / seek / leave             │        │                          │
 │            ▼                                         │        │                          │
 │  Bridge 127.0.0.1:8765 → CastSync ── HTTP :8888 ─────┼───────▶│  Playlet (YouTube)       │
 │                         └─ ECP :8060 (state, keys) ──┼───────▶│                          │
 └──────────────────────────────────────────────────────┘        └──────────────────────────┘
```

- **Audio:** the app authenticates to the Roku's ECP WebSocket the way the official mobile app does, then asks for the audio stream to be sent to the PC as RTP/Opus. It keeps the stream alive with RTCP receiver reports. FFmpeg decodes it to PCM, and the app applies volume and plays it.
- **Web:** Brave runs headless (`--headless=new`, no window at all) with a private profile. The app drives it over the Chrome DevTools Protocol (`web_guard.py`): it streams the page as JPEG frames that `web_view.py` draws on a canvas and sends your mouse and keyboard back, closes pop-ups, stops scripted redirects, removes click traps, and records every video stream the page loads with the headers the browser used. `stream_finder.py` discards the decoys and ranks what's left. `media_relay.py` serves the winner to the Roku, replaying those headers and rewriting HLS and DASH manifests so every segment also comes through the PC, and RokuCast plays it. With Upscale or Subtitles on, `upscaler.py` has FFmpeg read the stream instead (`subtitles.py` fetches the chosen track and draws it with libass), run the chosen shader through libplacebo (Vulkan), encode HEVC with NVENC, and the relay serves those pieces as a finished HLS playlist that's filled in as the TV asks. Chrome-family browsers only allow remote debugging on a non-default profile, which is one reason the Web tab has its own. A new profile never downloads Brave's ad filters by itself, so they're copied in from your normal Brave profile on each launch (`brave_profile.py`).
- **YouTube:** Chrome runs in app mode with your own profile and is re-parented into the app window. The extension watches the main YouTube player (never hover previews or ads) and reports events to the app over a local bridge that only the extension can use. The app mirrors those events to Playlet. Playlet seeks by reloading at a timestamp, which costs a 2–3 s rebuffer, so seeks are aimed ahead by a learned amount, and the muted PC player is then nudged onto the TV's timeline.

## Project structure

```
roku-listen/
├── roku_listen_gui.py     # desktop app: window, audio, remote control, auto-cast sync (and Resync)
├── ui_kit.py              # the look: colours, font, anti-aliased buttons, sliders, switches, chips, tooltips
├── roku_find.py           # finds Rokus on the network (SSDP) for the Find button
├── roku_listen.py         # protocol helpers + command-line client
├── yt_embed.py            # launches and embeds Chrome in the YouTube tab
├── yt_bridge.py           # local bridge between the app and the extension
├── web_tab.py             # Web tab: toolbar, Brave lifecycle, Cast flow
├── web_view.py            # draws the headless browser's page and forwards mouse / keyboard
├── web_sync.py            # keeps the TV following the page's video (pause, play, scrub)
├── tv_clock.py            # where the TV really is, from position readings that lag in steps (both syncs use it)
├── web_guard.py           # DevTools guard: pop-ups, redirects, click traps, stream capture, screencast
├── stream_finder.py       # picks the real video out of the decoys (pure logic)
├── media_relay.py         # serves a stream to the Roku with the site's headers; HLS rewriting
├── tv_apps.py             # the quick app row: the Roku's app list and pinning (pure logic)
├── phone_store.py         # bookmarks and history for the phone's Browse tab (a small JSON file)
├── phone_remote.py        # phone remote: tiny LAN web server + the home-screen web app it serves (remote, browser, YouTube/Playlet, cast, apps)
├── discord_presence.py    # optional Discord status for what's on the TV (Web and YouTube)
├── upscaler.py            # 4K upscaling and subtitle burn-in on the PC's GPU, for the Web tab's casts
├── subtitles.py           # finds a page's subtitle tracks and turns the chosen one into styled ASS
├── brave_profile.py       # the Web tab's private Brave profile and ad-filter seeding
├── drm.py                 # refuses DRM-protected sites and streams
├── tests/                 # unit tests, plus a hostile test site (guard_smoke.py)
├── extension/             # "Playlet Cast" Chrome extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js      # cast button, bridge relay, injection into open tabs
│   ├── feed.js            # auto-cast: mirrors the YouTube player to the TV
│   └── popup.* / options.*
├── assets/                # Roboto font (Apache 2.0) and icons
├── docs/screenshots/
├── build.ps1              # builds dist\CapoCast.exe + extension zip
└── requirements.txt
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status says **Reload Playlet Cast in chrome://extensions** | Check that the extension is loaded and **Developer mode** is on, then click its reload icon. |
| **Port 8765 is busy** | Another copy of CapoCast is running. Close it. Opening the app again normally just brings the existing window forward. |
| **Can't reach Playlet on the Roku** | Open Playlet on the TV and check the Roku IP. |
| Remote buttons or **Type on TV** do nothing | On the Roku: *Settings → System → Advanced system settings → Control by mobile apps → Network access → Default/Permissive*. |
| Connected but no sound | Allow CapoCast through Windows Firewall on private networks, and check that FFmpeg is on your `PATH`. |
| Crackling audio | Raise **Roku buffer** (try 500–700). |
| Status says **No audio from the Roku** | The TV stopped sending sound (it may be off, or on an input that has none). It recovers by itself when sound returns. Once a minute the log has an `rtp:` line counting packets that never arrived. |
| Status says **Audio device lost, reconnecting…** | The sound device went away (unplugged, or a driver reset). CapoCast retries for 30 s; pick another device in **Output** if it doesn't come back. |
| The Apps tiles show names, not pictures | The Roku wasn't reachable yet. They fill in by themselves and retry every 30 s. |
| Web tab says **Ad filters missing** | Open Brave normally once so it downloads its filters, then reopen the Web tab (or restart the app). |
| Web tab says **The TV never asked this PC for the video** | Allow CapoCast (or Python) through Windows Firewall on **private networks**; the Roku connects to TCP 8766–8770. Also check the Roku IP on the Listen tab. |
| Web tab says **No video found yet** | Press play on the video first, then Cast. For pages whose video can't be seen, install [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`winget install yt-dlp.yt-dlp`) and Cast tries that as a fallback. |
| **The RokuCast channel isn't installed** | The Web tab plays through the RokuCast channel sideloaded in your Roku's developer mode. |
| **The Roku refused the command** | Same Roku setting as above: *Control by mobile apps → Network access → Permissive*. |
| Web tab says **Upscaling failed, casting it as it is** | The reason is in the log. Check that the NVIDIA driver is up to date and FFmpeg is the full build (`winget install Gyan.FFmpeg`). Some sites also refuse a second reader, and those can only be cast plainly. |
| Web tab says **Upscaling is running slower than the video** | The site may be slow, or the GPU busy. Close what's using the GPU, or click **Upscale** to a lighter setting (**Anime4K** rather than **Anime4K HQ**, or **Off**) and Cast again. |
| The TV buffers with Upscale on, but no warning shows | Probably the network. The log's `stream needs … TV took it at …` lines show it: if the second number is close to or below the first, Wi-Fi can't carry the stream. |

The app's log lives at `%APPDATA%\RokuListen.log` and its settings at `%APPDATA%\RokuListen.json`.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

- [**RPListening**](https://github.com/runz0rd/RPListening), originally by William Seemann, for the reverse-engineered Roku private-listening protocol.
- [**Playlet**](https://github.com/iBicha/playlet) by iBicha, for the open-source YouTube client for Roku and its local web API.
- [**Anime4K**](https://github.com/bloc97/Anime4K) by bloc97, for the anime upscaling shaders, licensed under MIT (see `assets/anime4k/`).
- [**FSRCNNX**](https://github.com/igv/FSRCNN-TensorFlow) by igv, for the live-action upscaling shader, licensed under LGPL 3.0 (see `assets/fsrcnnx/`).
- [**Roboto**](https://fonts.google.com/specimen/Roboto), licensed under Apache 2.0 (see `assets/Roboto-LICENSE.txt`).

## License

[GPL-3.0](LICENSE) © CapoCast contributors.

*CapoCast is an independent project. It is not affiliated with, endorsed by, or sponsored by Roku, Inc., Google LLC or YouTube. Roku, YouTube and their logos are trademarks of their respective owners.*
