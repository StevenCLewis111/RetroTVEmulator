# Retro TV Emulator

A simulated "old cable box" desktop app for Windows. It boots into a fake CRT‑era
TV experience: you sit back and channel‑surf through 40 programmable stations that
auto‑schedule your own movies/shows like a real broadcast network, plus a built‑in
retro game console (18 emulated systems + DVD player), a TV Guide channel, and
music‑visualizer music channels. Everything is navigated with a keyboard or a
game controller (only on channel 03) — a mouse is only required for the file explorer.

\- launched games are controlled by controller only

**Read this top-to-bottom the first time.** The first few sections cover the stuff
that actually determines whether your setup works right — where files go and how
your TV stations get built. Everything past the Settings Menu Overview is good to
know but won't break anything if you skim it.

\---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Two Ways to Add Content — Read This First](#2-two-ways-to-add-content--read-this-first)
3. [Content Setup Cheat Sheet](#3-content-setup-cheat-sheet)
4. [Folder Layout in Full](#4-folder-layout-in-full)
5. [Building Your TV Stations — Series, Movies \& Naming](#5-building-your-tv-stations--series-movies--naming)
6. [Channel Numbering](#6-channel-numbering)
7. [Settings Menu Overview](#7-settings-menu-overview)
8. [Themes](#8-themes)
9. [Aspect Ratio \& the 4:3 / Bezel Test Mode](#9-aspect-ratio--the-43--bezel-test-mode)
10. [DVD Player](#10-dvd-player)
11. [Channel 03 — The Games Deck](#11-channel-03--the-games-deck)
12. [Channel 04 — The TV Guide](#12-channel-04--the-tv-guide)
13. [Full Controls Reference](#13-full-controls-reference)
14. [Logs](#14-logs)
15. [Quick Start Recap](#15-quick-start-recap)
16. [Changelog](#16-changelog)

\---

## 1\. Requirements

* **Windows.** The app talks directly to Win32 (window chrome, DPI awareness, a
low‑level keyboard hook for IR remotes, per‑process volume via WASAPI), so it
will not run on macOS/Linux. It does run on Proton on Linux but still testing controller support.
* Nothing else to install — the VLC runtime (`libvlc.dll`, `libvlccore.dll`,
`plugins/`) ships bundled inside the app's `\_internal\\vlc` folder, so a
separate VLC Media Player install is **not** required. Just keep that folder
 — don't delete or move it. Make a shortcut and move it to Desktop if you want a
desktop icon.
* A game controller is optional but the only way to play games — the keyboard
does not control launched games. Xbox/DirectInput‑style pads are supported for
the Games channel and DVD playback.

\---

## 2\. Two Ways to Add Content — Read This First

Every piece of content this app uses — ROMs, cores, BIOS, splash art, series,
movies, music — can be added **two different ways**, and picking the right one
up front saves you a headache later.

### Option A: Drop‑In (Standalone / Portable)

Copy files directly into the matching folder inside `main/`, next to the `.exe` in the \_internal folder.
This is the way to go if you want the whole setup to be **plug‑and‑play on any
computer** — copy the entire app folder to a USB drive or another PC, and
everything just works, because every file it needs lives inside its own folder
tree. Nothing points outside the `main/` folder.

### Option B: Point at Files Anywhere (File Explorer / Picker)

Almost every content menu in Settings has a **built‑in file browser** — press
Enter on a "Select…" row and it opens a full file/folder navigator you can use
to browse your entire drive (or an external one) and check off files or pick a
folder, without moving or copying anything. This is faster if you've already
got a big library sitting somewhere else and don't want to duplicate gigabytes
of video onto a second drive.

**The trade‑off:** a path picked this way is tied to that exact folder on that
exact PC/drive. Move the app to another computer (or rename/relocate the source
folder) and that content won't be found until you re‑point it. If portability
matters to you, prefer Option A; if convenience and disk space matter more,
Option B is built for that.

You can mix both freely — e.g. keep cores/BIOS drop‑in (small, rarely change)
while pointing your Series/Movies channels at a large external drive.

\---

## 3\. Content Setup Cheat Sheet

|Content type|Default drop‑in folder|Picker available?|
|-|-|-|
|Libretro core `.dll`|`main/cores/<CONSOLE>/`|Yes — Games tab → console → Select Libretro Core|
|ROMs|`main/roms/<CONSOLE>/`|Yes — Games tab → MENU → scan for roms  or Games tab → console add/remove roms |
|BIOS (PSX only)|`main/cores/PSX/system/`|Yes — Games tab → PSX → Add BIOS|
|Splash image (per console/DVD/menu)|`main/images/splash screen images/<CONSOLE>/`|Yes — Games tab → console → Select Splash Image|
|Splash video (per console/DVD)|`main/videos/splash screen videos/<CONSOLE>/`|Yes — Games tab → console → Select Splash Video|
|Menu background image|`main/images/splash screen images/MENU/`|Yes — Games tab → Menu Background|
|Console logo|`main/images/logos/.png`|These are not for changing|
|Bezel/border overlay art|`main/roms/<CONSOLE>/bezel/bezel.png`|Drop‑in only|
|Whole‑screen 4:3 bezel (Test Mode)|`main/images/bezel/`|Drop‑in only|
|Series/Movies per channel|Anywhere — set per channel|Yes — Channels tab → channel → Content folder(s)|
|Commercials / Intros / Outros|Anywhere — set per channel|Yes — Channels tab → channel → Commercials \& Extras|
|Visualizer audio tracks|Anywhere — set per channel|Yes — Channels tab → channel → Audio Tracks folder|
|Built‑in SFX/menu music|`main/audio/`|Drop‑in only|

\---

## 4\. Folder Layout in Full

Everything lives in a `main/` folder next to the app's `.py`/`.exe`. Folders the
app needs are created automatically the first time it needs them — ROMs, cores,
and BIOS still have to be supplied by you:

```
main/
├─ cores/
│  ├─ NES/         <core>\_libretro.dll   (+ optional system/ subfolder)
│  ├─ SNES/        ...
│  ├─ GENESIS/     ...
│  ├─ N64/         ...
│  ├─ PSX/         ...
│  ├─ MAME/        ...
│  ├─ GB/  GBC/  GBA/  GG/  NGP/  NGPC/  NG/
│  ├─ A2600/  A7800/  AJAGUAR/  ALYNX/  32X/  MASTERSYSTEM/
├─ roms/
│  ├─ NES/   \*.nes / .zip / .7z              (+ saves/, bezel/bezel.png)
│  ├─ SNES/  \*.smc, \*.sfc, .zip, .7z
│  ├─ GENESIS/ \*.md, .gen, .bin, .smd, .zip, .7z
│  ├─ N64/   \*.n64, .z64, .v64, .zip, .7z
│  ├─ PSX/   \*.cue, .bin, .iso, .chd, .pbp, .img, .zip, .7z
│  ├─ MAME/  \*.zip, .chd, .7z
│  ├─ GB/ \*.gb    GBC/ \*.gbc, .gb    GBA/ \*.gba    GG/ \*.gg
│  ├─ NGP/ \*.ngp, .ngc    NGPC/ \*.ngc, .ngp    NG/ \*.zip, .7z
│  ├─ A2600/ \*.a26, .bin    A7800/ \*.a78, .bin
│  ├─ AJAGUAR/ \*.j64, .jag, .rom    ALYNX/ \*.lnx
│  ├─ 32X/ \*.32x, .bin, .smd, .md    MASTERSYSTEM/ \*.sms
│  └─ DVD/  \*.mp4, .mkv, .iso, .vob, .mpg, .mpeg, .avi, .zip, .7z
├─ bios/               shared BIOS dumps — currently only needed for PSX
│                       (e.g. scph5500.bin / scph5501.bin / scph5502.bin)
├─ images/
│  ├─ logos/            <CONSOLE>.png console logos for the Games menu
│  ├─ bezel/             4:3 "whole‑screen" CRT bezel art (Test Mode)
│  ├─ splash screen images/<CONSOLE>/   per‑console splash art
│  ├─ splash screen images/MENU/        menu background image
│  └─ Wireframes/       per‑console placeholder art shown in Controller Mapping
├─ videos/
│  └─ splash screen videos/<CONSOLE>/   per‑console splash video (no MENU folder — see below)
└─ audio/               gamemusic.mp3, static.wav, etc. (built‑in SFX/menu music)
```

**Per‑console notes**

* Every console folder under `main/cores/<CONSOLE>/` needs a libretro core
`.dll` (e.g. `mgba\_libretro.dll`, `snes9x\_libretro.dll`). The app scans that
folder for any file ending in `\_libretro.dll` (or, failing that, any `.dll`)
and loads the first match.
* **PSX** BIOS file goes in `main/bios/`, since PSX cores need real console BIOS
dumps rather than a per‑core scratch folder.
* Save files/states live under `main/roms/<CONSOLE>/saves/profile{1,2,3}/` — see
**Save Profiles** below.
* An optional `main/roms/<CONSOLE>/bezel/bezel.png` gives that console a
matching CRT/handheld bezel overlay in‑game (falls back to no overlay if
missing — this never crashes). Older `border/border.png` still works as a
fallback if you haven't renamed it, but new setups should use `bezel/bezel.png`.
* **DVD** isn't an emulator — it's a video player for movie‑style files/discs and
has no core, BIOS, controller mapping, or save profile of its own.
* **MENU** only has a splash **image** option (used as the menu background) —
there's no splash video slot for it, so no `videos/splash screen videos/MENU/`
folder is created.

\---

## 5\. Building Your TV Stations — Series, Movies \& Naming

This is the section that actually determines whether a channel plays your
shows the way you expect. How you name folders and files controls how the app
groups shows and orders episodes.

### Franchise / spin‑off bundling

Normally each show folder is its own lineup entry. If you'd rather a
multi‑series franchise (Stargate SG‑1 + Atlantis + Universe, for example) air
as **one** entry — playing all of the first series, then all of the next,
instead of getting three separate back‑to‑back slots — file the spin‑offs
like this:

```
Stargate/
├─ 1. Stargate SG1/
│  ├─ Season 1/  ...
│  └─ Season 2/  ...
├─ 2. Stargate Atlantis/
│  └─ Season 1/  ...
└─ 3. Stargate Universe/
   └─ Season 1/  ...
```

Two things both have to be true for the app to bundle them:

1. The spin‑off folders share one parent folder ("Stargate").
2. Every spin‑off folder's name actually starts with the parent's name —
"Stargate SG1", "Stargate Atlantis", etc. A parent folder that's just a
generic library bucket ("TV Shows", "Comedies") with unrelated shows inside it
will **not** bundle them — the child names have to carry the shared franchise
name, not just share a folder.

Once both are true, the whole franchise shows up as a single entry named after
the shared parent ("Stargate"), and plays each spin‑off fully through before
moving to the next.

**Play order within a bundle:** put a number and a separator (`1.`, `2 -`,
`3)`, `4\_`) at the start of each spin‑off folder's name to pin the order —
`1. Stargate SG1` always plays before `2. Stargate Atlantis`. No numbers at all
still bundles them, just in a stable best‑guess (alphabetical) order instead of
one you control — so number them if the order matters to you.

### Season \& episode order

Whether a show is bundled or not, its own seasons always play in numeric order
regardless of folder name length — `Season 2` before `Season 10`, not the
other way around — as long as each season folder is named with the word
"Season" (or `S01`/`S02`) followed by its number.

Files inside a season folder play in season/episode order using whatever of
these patterns is in the filename: `S01E05`, `Season 1 Episode 5`, or a lone
`Episode 5` / `Ep5`.

### Placing a movie relative to episodes

A movie doesn't need any special tag to be *recognized* as a movie (that's
automatic, based on its runtime — anything over \~80 minutes) — but tagging it
with an episode number is how you control **where it plays**:

* **Before the whole season starts:** give it `Episode 0` of that season (e.g.
`Stargate.S01E00.mkv`) — it plays before `S01E01`.
* **Right after a specific episode:** tag it with THAT episode's own number,
e.g. `Stargate.S01E17.Movie.mkv` alongside the real `Stargate.S01E17.mkv`. Two
files sharing the same episode number sort next to each other, and the plain
filename sorts before one with extra text appended — so the real episode 17
plays, then the movie, then episode 18 picks up normally.
* **At the very end of a season:** give it the next unused episode number —
e.g. real episodes end at `S02E29`, so `Stargate.S02E30.Movie.mkv` plays right
after `S02E29` and before `Season 3` begins.
* **Between two seasons, filed under the NEXT season instead:** tag it
`Episode 0` of the season that follows, e.g. `Stargate.S03E00.Movie.mkv` in the
`Season 3` folder — it plays before `S03E01`.

Don't drop an untagged file (no season/episode pattern at all) loose inside a
show or spin‑off folder expecting it to land in a particular spot — with no
recognized tag it always sorts to the very END of that show's whole episode
list. Give it a real `SxxExx` tag using the placement rules above instead.

### Scheduling Mode — how shows take turns

Set per channel (Channels tab → channel → Scheduling Mode). This controls how
a block's shows take turns, not how much airs overall (that's Block Length,
below). Every mode shuffles the shows into a channel‑seeded order once and
reuses it until you add/remove shows, so runs stay stable day to day instead
of reshuffling every rebuild:

* **Random Slots** *(default)* — each show gets a proportional run of 1–3
back‑to‑back episodes before handing off to the next show, weighted by how
many episodes that show has. Gives the most "real channel" feel.
* **One Slot** — strict round‑robin: one episode of Show A, one of Show B, one
of Show C, then back to Show A for its next episode, and so on.
* **Two Slots** — same round‑robin idea, but each show gets two episodes per
turn instead of one before handing off.
* **Marathon** — not a per‑block mode at all: it plays one fixed, fully
ordered catalog on a continuous clock with no daily reshuffling and no
Morning/Evening/Night split. Marathon hides Episode Order and Block Length
entirely because of this.

Movies are pulled out of this rotation automatically and re‑inserted on their
own schedule: a movie‑only channel fills the whole block with movies, a mixed
channel gives movies a time budget proportional to how large the movie pool is
versus the series library, and **Two Slots** specifically pairs movie picks
into back‑to‑back "double feature" slots.

**Marathon‑specific notes:** a bundled franchise still plays as one
uninterrupted run in Marathon. Load multiple *unrelated* shows into the same
Marathon channel and each one plays all the way through before the next one
starts — but which unrelated show goes before which other one is decided by a
fixed, per‑channel shuffle, not by folder name. Naming controls order *within*
one show or bundle; it doesn't control order *between* separate shows in
Marathon.

### Block Length — how the day is divided

|Block Length|Windows|Content folders shown|
|-|-|-|
|**24‑Hour (Full Day)**|One continuous 24‑hour pool|Full Day|
|**8‑Hour Blocks**|Morning (4 AM–12 PM), Evening (12 PM–8 PM), Night (8 PM–4 AM)|Morning / Evening / Night|
|**4‑Hour Blocks**|12–4am, 4–8am, 8–12pm, 12–4pm, 4–8pm, 8–12am|All six 4‑hour buckets|

Each folder is independent — content filed under Morning won't air during
Evening, etc. Press Enter on the Block Length row to cycle 24‑Hour → 8‑Hour →
4‑Hour → back; switching modes only reshapes what airs *next* — whatever's
currently playing finishes uninterrupted.

### Commercials, Intros \& Outros

Separate file pools for **Commercials**, **Intros**, and **Outros**, plus a
placement choice — **Interrupt show** (cut in around the half‑hour) or **End
of show only**. *(Intros and outros are unfinished/untested features.)*

### Supported file types

Video (TV/DVD content): `.mp4 .mkv .avi .mov .wmv .webm .m4v .flv` (plus
DVD‑specific `.iso .vob .mpg .mpeg .zip .7z`). Audio (visualizer channels):
`.mp3 .wav .ogg .flac .m4a .aac .wma`.

\---

## 6\. Channel Numbering

|Channel|What it is|
|-|-|
|**03**|Games (emulator + DVD deck) — toggle on/off in Settings → System|
|**04**|TV Guide — toggle on/off in Settings → System|
|**05–44**|40 user‑programmable "stations," each independently scheduled|

Only Channel 05 is active out of the box on a fresh install; everything else
starts OFF until you turn it on and give it content in Settings → Channels. At
least one content channel (or Channel 03/04) must always stay enabled — the
app won't let you disable the last one.

Jump straight to a channel by typing its two‑digit number on the keyboard's
number row or numpad (e.g. `0` `5` → Channel 05). The digits are buffered for
\~2.5 seconds before being applied.

\---

## 7\. Settings Menu Overview

Open/close with **M**. Tabs across the top: **System**, **Channels**, **Games**
(only shown while the Games channel is enabled), **Video**, **Theme**. Kiosk
Mode hides the Channels tab entirely.

### System tab

|Toggle|What it does|
|-|-|
|Channel 03 (Games)|Enables/disables channel‑surfing to the Games deck. The last remaining enabled channel (03, 04, or a TV/MUSIC channel) can't be turned off — you always need at least one channel left to land on.|
|Channel 04 (TV Guide)|Enables/disables channel‑surfing to the TV Guide (Section 12).|
|Controls Menu on Start|Shows the Controls help screen automatically on boot|
|Boot on Start|Makes the app itself the Windows shell (replacing `explorer.exe`) for your Windows user, so it launches straight to fullscreen before the desktop ever appears — see below|
|Kiosk Mode|Locks out settings/menus that touch the filesystem, for a hands‑off "just watch/play" setup — see below|

Buttons: **Remote Remapping**, **Reset All Settings**, **Exit Program** (and
**Turn Off Computer**, which appears once the app has taken over as the shell
— see Boot on Start below).

**Kiosk Mode** removes: the entire Channels tab (no re‑scheduling from inside
the app), and the "Select Libretro Core" row in every console's Games
sub‑menu (no browsing the filesystem to swap in a different core). It also
hides the **Set Music** picker on the Games home screen's Menu Music option
(the toggle itself still works, it just can't be re‑pointed at new files).
Kiosk Mode does **not** by itself remove desktop/Explorer access — that's
what Boot on Start controls.

**Boot on Start** is the real "shell replacement" toggle: turning it on
writes the app's own launch command (with a `--as-shell` flag) into the
Windows registry key that controls what runs as the shell (`HKCU\\...\\Winlogon\\ Shell`), in place of `explorer.exe`. From then on, that Windows user account
boots straight into the app — full‑screen, no desktop, no taskbar — every
time it logs in. Turning it back off restores `explorer.exe` as the shell. The
`--as-shell` flag is also what makes the **Turn Off Computer** button appear
in this menu (there's no taskbar to shut down from otherwise). If Boot on
Start is on, the only way back to a normal desktop is turning the toggle off
from inside the app, or powering off — there's nothing to alt‑tab to.

**Remote Remapping** lets you rebind every core action — Nav Up/Down/Left/
Right, Ch Up/Down, Volume Up/Down, Select, Back/Minimize, Mute, Menu — and
separately rebind the 10 number‑dial digits, to any physical key (handy for IR
remotes whose buttons send unusual scancodes). WASD/arrow keys can't be
reassigned. Press a key to bind it; a quick double‑tap of Escape/Back cancels a
capture in progress. DVD has its own separate remapping panel instead (Games
tab → DVD → Button Mapping), covering DVD‑specific transport actions rather
than these core actions.

### Channels tab

A 5×8 grid of channels 05–44, each cell showing its channel number and a
green/red dot for on/off. Selecting a channel opens its scheduling sub‑menu:

* **Status** — on/off for this channel (locked on if it's the last active
channel — you must enable another before turning this one off).
* **Mode: TV / MUSIC** — TV plays your video library on a schedule; MUSIC turns
the channel into an audio‑reactive **visualizer** station instead.
* **Name** — a custom on‑screen channel name (up to 12 characters), edited in
place.

**TV mode** scheduling options:

|Setting|Choices|Notes|
|-|-|-|
|Scheduling Mode|Random Slots, One Slot, Two Slots, Marathon|See Section 5|
|Pair Shorts Under|0 (off) – 30 minutes|Auto‑pairs back‑to‑back short episodes under this length into one block|
|Commercials|On/Off|Opens the Commercials \& Extras sub‑menu|
|Episode Order|Sequential / Random|Per‑show "what plays next" order (hidden in Marathon)|
|Block Length|24‑Hour / 8‑Hour / 4‑Hour|See Section 5 (hidden in Marathon)|
|Content folder(s)|Depends on Block Length|Pick the video files/folders that populate each slot|
|Holiday Schedule Overrides|Halloween, Christmas, Valentine's Day, Thanksgiving, New Year's Eve|Optional swap‑in content for those calendar days|
|Change Current Playing Audio Track|—|Only usable while this channel is live; fixes a file defaulting to the wrong embedded audio track|
|Clear All|—|Wipes every scheduled file for this channel|

**MUSIC mode**: pick a **Visualizer Engine Style** — Random, or one of
PrismShards, LiquidChrome, PulseRings, WarpTunnel, GlowBraid, EqualizerGrid,
ParticleFlow, NeonStarfield — and an **Audio Tracks folder**.

**4:3 Mode / Bezel** (bottom‑left of the Channels tab, 16:9 displays only): see
Section 9.

### Games tab

Pick a console from the list to open its sub‑menu. For every emulated console:

* **Status** — enable/disable this console (the last enabled console is locked
on).
* **Select Libretro Core (.dll)** — browse to a specific core file (hidden in
Kiosk Mode).
* **Controller Mapping** — assign pad buttons for this console.
* **Save Profiles 1 / 2 / 3** — three independent save slots per console.
Only the **active** profile's options are editable (the other two grey out);
switch which one is active from that profile's own screen. Each profile has:

  * **Save Now / Load Now** — manual save‑state save/load for that slot.
  * **Auto‑Save** — when on, a state is saved to that profile's slot
automatically whenever the game is put away (switching games/consoles,
quitting to the ROM list, or the app losing focus) — off by default.
  * **Auto‑Load** — when on, that profile's saved state (if one exists) is
loaded automatically the moment the game launches, instead of a fresh
boot — off by default.
* **Change Disc** *(PSX only)* — cycles between detected discs of a multi‑disc
game (filenames need a `(Disc 1)` / `(CD 2)` / `\[Disc 2 of 2]` style marker to
be recognized as siblings). *(Untested feature.)*

**DVD** isn't an emulator, so its sub‑menu is just **Status** plus a **Button
Mapping** row — no core selection, controller mapping, or save profiles.

Supported consoles:

|Console|Label|Extensions|
|-|-|-|
|DVD|DVD Video Interactive|.mp4, .mkv, .iso, .vob, .mpg, .mpeg, .avi, .zip, .7z|
|A2600|Atari 2600|.a26, .bin, .zip, .7z|
|A7800|Atari 7800|.a78, .bin, .zip, .7z|
|AJAGUAR|Atari Jaguar|.j64, .jag, .rom, .zip, .7z|
|ALYNX|Atari Lynx|.lnx, .zip, .7z|
|GB|Game Boy|.gb, .zip, .7z|
|GBA|Game Boy Advance|.gba, .zip, .7z|
|GBC|Game Boy Color|.gbc, .gb, .zip, .7z|
|GG|Game Gear|.gg, .zip, .7z|
|MAME|MAME Arcade|.zip, .chd, .7z|
|NG|Neo Geo|.zip, .7z|
|NGP|Neo Geo Pocket|.ngp, .ngc, .zip, .7z|
|NGPC|Neo Geo Pocket Color|.ngc, .ngp, .zip, .7z|
|N64|Nintendo 64|.n64, .z64, .v64, .zip, .7z|
|NES|Nintendo Entertainment System|.nes, .zip, .7z|
|PSX|PlayStation|.cue, .bin, .iso, .chd, .pbp, .img, .zip, .7z|
|32X|Sega 32X|.32x, .bin, .smd, .md, .zip, .7z|
|GENESIS|Sega Genesis|.md, .gen, .bin, .smd, .zip, .7z|
|MASTERSYSTEM|Sega Master System|.sms, .zip, .7z|
|SNES|Super Nintendo|.smc, .sfc, .zip, .7z|

Every console except DVD also has a **Screen Size** option per profile (Full /
75% / 50%) and, on most consoles, an optional TV/handheld **bezel overlay**
pulled from `roms/<CONSOLE>/bezel/bezel.png`.

### Video tab

Five sliders, 0–100, applied as a real‑time image filter over whatever's
playing: **Brightness, Contrast, Color, Sharpness, Tint.**

### Theme tab

See Section 8.

\---

## 8\. Themes

Settings → Theme:

* **Menu Transparency** — 0–100%, how see‑through the Settings/Controls
overlays are.
* **Background Color** — hue picker for the menu background.
* **Border/Text Color** — hue picker for the neon accent/border color used
throughout menus and the TV Guide.
* **Channel Transition** — **Black** (grey flash) or **Static** (TV‑static
flash + noise burst) between channel changes.

These are global — they affect every menu screen and the on‑screen accents
everywhere, not just one channel.

\---

## 9\. Aspect Ratio \& the 4:3 / Bezel Test Mode

**4:3 Mode / Bezel** (Channels tab, bottom‑left, only shown on 16:9 displays)
lets you preview how the app behaves on a real 4:3 CRT without changing your
actual Windows display settings — useful for checking letterboxing and the
console CRT‑bezel borders before you own a real 4:3 monitor. The bezel overlay
only shows once 4:3 Mode is turned on, and is pulled from `main/images/bezel/`
(`bezel.png`, falling back to `border.png` if that's what you already have).

Per‑console **Screen Size** (Full / 75% / 50%, set per save profile in the
Games tab) is separate from this — it controls how large the emulated picture
renders inside whatever frame is active, useful for leaving room around the
image for a bezel overlay.

\---

## 10\. DVD Player

DVD is Channel 03's non‑emulator option — a straightforward video player for
movie files or ripped discs, with support for on‑disc interactive menus on
ISO images.

* **Playback** uses your keyboard's dedicated media keys (Play/Pause, Rewind,
Fast‑Forward) by default, not W/A/S/D. If your keyboard doesn't have those
keys, remap them (see below).
* **W/A/S/D** are reserved for navigating an on‑disc menu and only do anything
while a disc's own interactive menu is on screen — inert during normal
playback.
* **Captions** toggle with `V` (or D‑pad Up on a controller).
* **Backspace** returns to the disc's own menu — ISO discs only; does nothing
on a plain video file.
* Every DVD transport action (Play/Pause, Rewind, Fast‑Forward, Captions,
Return‑to‑menu) can be individually **remapped** — go to **Settings → Games →
DVD → Button Mapping** (a separate panel from System's Remote Remapping, so
remapping DVD transport never changes anything else).

See the full control table in Section 12 for exact tap/hold behavior.

\---

## 11\. Channel 03 — The Games Deck

Channel 03 is the emulator + DVD hub. Beyond just adding ROMs, there's real
room to customize how it looks and feels per console:

### Visuals

* **Console logo** — drop a `<CONSOLE>.png` into `main/images/logos/` to show
a custom logo for that console in the Games menu.
* **Splash image/video** — a brief intro shown before that console (or DVD)
loads. Turn on with **Splash On/Off**, then use **Select Splash Image** /
**Select Splash Video** in that console's sub‑menu — both open the built‑in
file browser pointed straight at that console's splash folder. MENU only
supports a splash image (used as the overall menu background), not video.
* **Bezel overlay** — an in‑game CRT/handheld frame around the emulated
picture, pulled from `main/roms/<CONSOLE>/bezel/bezel.png`. Falls back to no
overlay if missing, so it's always safe to skip.
* **Screen Size** — Full / 75% / 50%, set per save profile, controls how large
the picture renders inside the frame/bezel.
* **Wireframe placeholder art** — shown on the Controller Mapping screen when
no logo/wireframe has been supplied yet, so you always see where custom art
would go (`main/images/Wireframes/<CONSOLE>\_wireframe.png`).

### Color / text

Channel 03's menu chrome (background tint, border/accent color, transparency)
follows the same global **Theme** settings covered in Section 8 — there's no
separate color scheme just for the Games deck.

### Controls

Controller mapping is per‑console (Games tab → console → Controller Mapping).
DVD has its own separate remapping panel (Settings → Games → DVD → Button
Mapping) instead, since it's transport controls rather than a game pad layout.
See Section 13 for the default bindings.

\---

## 12\. Channel 04 — The TV Guide

Channel 04 is a grid guide: one row per active TV/MUSIC channel (05–44), one
column per half‑hour of the day.

### Navigation

|Key|Action|
|-|-|
|W / S|Move the highlighted row up / down one channel (wraps top‑to‑bottom)|
|A / D|Shift the visible time columns left / right, half‑hour at a time|
|Enter|Tune to whichever channel is currently highlighted|

Only WASD moves the highlight — the arrow keys don't navigate the grid here
(they're read elsewhere, so pressing one still counts as activity and resets
the inactivity timer below, it just won't move the cursor).

### It mirrors your actual schedules

The guide isn't a separate listing you maintain — it reads live off the same
scheduling engine that drives the channels themselves. Whatever you've built
in Section 5 (series order, movie placement, Scheduling Mode, block length,
commercials) shows up automatically: each column shows what's actually
airing on that channel at that half‑hour, today, right now. There's nothing
extra to configure to make the guide "know" your lineup.

### The "now" column

The guide always opens on the column matching the real current time — that
column acts as a permanent "play now" marker (like a real EPG's live
indicator), and A/D shift you away from it to browse later/earlier slots
without losing track of it.

### Live preview

Highlighting a row plays a small preview of that channel in real time —
seeked to the exact point its schedule says it should be at *right now*, not
started over from the beginning:

* **TV channels** preview silently (video only, no audio) — this avoids two
audio streams fighting when you're just channel‑browsing.
* **MUSIC/Visualizer channels** preview with audio, so you can actually hear
what you'd be tuning into.

Moving to a new row swaps the preview to that channel (a brief static burst
plays in Static theme mode, see Section 8); pressing **Enter** commits and
actually tunes to it, same as changing the channel from anywhere else.

### It remembers where you left off

The guide always opens back on the **current time column**, but the
**highlighted row** is remembered from the last time you were in the guide —
not necessarily the channel you're currently watching. Leave the highlight
sitting on Channel 12 and come back later (even after watching something
else in between), and Channel 12 is still highlighted, scrolled into view.

### Anti‑burn‑in timeout

If you sit on the TV Guide for **60 seconds** without pressing anything
(WASD, arrows, or Enter all count as activity and reset the clock), the app
automatically tunes away to whichever channel is currently highlighted —
this exists purely to stop a static grid from burning into a CRT/plasma
display, not as a "did you fall asleep" prompt. Any key press resets the
60‑second clock, so actively browsing never triggers it.

\---

## 13\. Full Controls Reference

### TV controls (any channel)

|Key|Action|
|-|-|
|Up / Down|Channel up / down|
|Left / Right|Volume down / up|
|N|Mute / unmute|
|M|Open / close the main Settings menu|
|C|Open / close the Controls help menu|
|Escape|Minimize the window|

### TV Guide (Channel 04)

|Key|Action|
|-|-|
|W / S|Scroll channels up / down|
|A / D|Scroll time slots left / right|
|Enter|Tune to the selected channel|

### Menu navigation (Settings menu, etc.)

|Key|Action|
|-|-|
|W / S|Navigate up / down|
|A / D|Navigate left / right|
|Enter|Select / confirm|
|Escape|Back / minimize|

### Games channel

|Keyboard|Action|Controller|Action|
|-|-|-|-|
|W / S|Navigate menus|D‑pad|Navigate menus|
|Enter|Select / confirm|A button|Select / confirm|
|Escape|Back / quit game|B button|Back|
|||Start / Select|Quit game|

### DVD player

|Keyboard|Action|Controller|Action|
|-|-|-|-|
|Media Play/Pause key|Play / pause|D‑pad Down|Play / pause|
|Media Rewind key|Tap: −30s. Hold ≥0.5s: rewind continuously|D‑pad Left|Same (tap/hold)|
|Media Fast‑Forward key|Tap: +30s. Hold ≥0.5s: fast‑forward continuously|D‑pad Right|Same (tap/hold)|
|V|Captions on/off|D‑pad Up|Captions on/off|
|Backspace|Return to disc menu (ISO discs only)|—|—|
|Escape|Quit to TV — opens a Yes/No confirm|B button|Same as Escape|
|A / D|Switch Yes/No selection in a confirm prompt|—|—|
|Enter|Confirm the Yes/No prompt, or select a highlighted disc‑menu option|A button|Same as Enter|
|W / A / S / D|Navigate an on‑disc interactive menu (ISO discs only)|D‑pad|Same, while an on‑disc menu is up|

\---

## 14\. Logs

Two plain‑text logs are written next to the app so problems can be diagnosed
after the fact:

* `crash\_log.txt` — full traceback of any uncaught exception (main thread or
background thread).
* `app\_log.txt` — rotating general log (2MB per file, 3 backups kept) for
warnings and notable events short of a crash.

\---

## 15\. Quick Start Recap

1. For the Games channel: add a libretro core `.dll` under
`main/cores/<CONSOLE>/`, ROMs under `main/roms/<CONSOLE>/`, and (PSX only)
BIOS dumps under `main/bios/`. Cores/BIOS/ROMs are auto‑detected from their
file location, but if you have several, pick a specific one in the Games menu.
2. Launch the app, press **M** for Settings to turn on channels/consoles and
assign content, and press **C** any time for the in‑app controls reference.

\---

## 16\. Changelog

Running log of notable fixes/changes made in this project, most recent first.

* **Video tab (Brightness/Contrast/Color/Sharpness/Tint) now applies to the DVD
player.** Previously the filter only touched TV channels and the Visualizer —
DVD/ISO/real‑disc playback drew straight from VLC with no filter at all. The
filter logic moved into `media.py` (shared by `game\_deck.py` and
`retro\_tv\_emulator.py`, avoiding a circular import) and is now applied to
every DVD‑mode frame, updating live while a slider is dragged.
* **Added Section 12 (Channel 04 — The TV Guide) and expanded the System tab
write‑up.** Documented the TV Guide's navigation, live preview behavior,
last‑highlighted‑row memory, and 60‑second anti‑burn‑in timeout; expanded the
System tab entry to cover Kiosk Mode, Boot on Start/shell takeover, and Save
Profile auto‑save/auto‑load.
* **Stopped scaffolding an unused `videos/splash screen videos/MENU/` folder on
every launch.** MENU only ever had a splash *image* option — there's no splash
video slot for it — so the boot‑time folder scaffolding no longer creates a
video folder for MENU specifically (it still gets its splash *images* folder).
If you already have an empty leftover `MENU` folder under `videos/splash screen videos/`, it's safe to delete by hand; the app won't recreate it.
* **Border → Bezel rename, with legacy fallback.** In‑game and whole‑screen CRT
overlay art now looks for a `bezel/` folder and `bezel.png` file first
(`main/roms/<CONSOLE>/bezel/bezel.png` and `main/images/bezel/`), falling back
to the old `border/`/`border.png` naming only if no `bezel` version exists yet.
Nothing is deleted or migrated automatically — existing `border` folders keep
working, but new setups should use `bezel`.
* **Fixed a loose file dropped directly in a show/spin‑off folder creating a
bogus duplicate entry.** A file sitting straight inside a show's own folder
(not inside any `Season N` subfolder) was getting grouped under a garbled show
name (the file's full disk path) instead of joining the real show. Fixed in
`group\_shows()` (`media.py`): that fallback case now resolves to the folder's
own name, so the file's episodes merge into the real show instead of splitting
off.
* **Marathon now plays whole shows/bundles through instead of folder‑by‑
folder.** Marathon now groups by SHOW the same bundle‑aware way the other
scheduling modes do, so a franchise bundle airs as one uninterrupted run, and
loading several unrelated shows into one Marathon channel plays each one fully
through before a stable, per‑channel shuffle picks which remaining show runs
next.
* **Spin‑off franchises can now be bundled into a single lineup entry.** Added
detection in `group\_shows()` for sibling series folders that share their
parent folder's name; when found, they're bundled into one entry that plays
each spin‑off fully through before the next.
* **README corrected against actual behavior.** Several things an earlier
README described didn't match the code (DVD control table, Games tab missing
DVD's simpler sub‑menu, missing 4‑Hour Blocks option, no real description of
the four Scheduling Modes) — all corrected from the actual scheduling engine
and input‑handling code.
* **8‑Hour block boundaries adjusted.** Morning/Evening/Night moved from 5
AM–1 PM / 1 PM–9 PM / 9 PM–5 AM to **4 AM–12 PM / 12 PM–8 PM / 8 PM–4 AM** so
each name better matches when it actually airs.
* **Background playback stalling behind the Settings menu (Video +
Commercial).** Any channel playing in the background while the Settings menu
was open would stop advancing the instant a clip finished or a scheduled
commercial break was due to cut in/out. Fixed so the live channel behind the
menu keeps advancing exactly like it does while you're actually watching it.

