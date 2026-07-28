# Retro TV Emulator

A simulated "old cable box" desktop app for Windows. It boots into a fake CRT‑era
TV experience: you sit back and channel‑surf through 40 programmable stations that
auto‑schedule your own movies/shows like a real broadcast network, plus a built‑in
retro game console (12 emulated systems + DVD player), a TV Guide channel, and
music‑visualizer "radio" channels. Everything is navigated with a keyboard or a
game controller — no mouse required.

\---

## 1\. Requirements

* **Windows.** The app talks directly to Win32 (window chrome, DPI awareness,
a low‑level keyboard hook for IR remotes, per‑process volume via WASAPI), so
it will not run on macOS/Linux.
* Nothing else to install — the VLC runtime (`libvlc.dll`, `libvlccore.dll`,
`plugins/`) ships bundled inside the app's `\\\_internal\\\\vlc` folder, so a
separate VLC Media Player install is **not** required. Just keep that
folder next to the `.exe` — don't delete or move it. create a short cut if you want a desktop icon to click on.
* A game controller is optional but the only way to play games. the keyboard does not control launched games. supported (Xbox/DirectInput‑style pads)
for the Games channel and DVD playback.

## 2\. Folder layout the app expects

Everything lives in a `main/` folder next to the app's `.py`/`.exe`. Create the
folders you need — missing ones are created automatically where possible, but
ROMs/BIOS/cores have to be supplied by you:

```
main/
├─ cores/
│  ├─ NES/         <core>\\\_libretro.dll   (+ optional system/ subfolder)
│  ├─ SNES/        ...
│  ├─ GENESIS/     ...
│  ├─ N64/         ...
│  ├─ PSX/         ...
│  ├─ MAME/        ...
│  ├─ GB/  GBC/  GBA/  GG/  NGP/
├─ roms/
│  ├─ NES/   \\\*.nes / .zip / .7z            (+ saves/, border/border.png)
│  ├─ SNES/  \\\*.smc, \\\*.sfc, .zip, .7z
│  ├─ GENESIS/ \\\*.md, .gen, .bin, .smd, .zip, .7z
│  ├─ N64/   \\\*.n64, .z64, .v64, .zip, .7z
│  ├─ PSX/   \\\*.cue, .bin, .iso, .chd, .pbp, .img, .zip, .7z
│  ├─ MAME/  \\\*.zip, .chd, .7z
│  ├─ GB/ \\\*.gb    GBC/ \\\*.gbc, .gb    GBA/ \\\*.gba    GG/ \\\*.gg    NGP/ \\\*.ngp, .ngc
│  └─ DVD/  \\\*.mp4, .mkv, .iso, .vob, .mpg, .mpeg, .avi, .zip, .7z
├─ bios/            shared BIOS dumps — currently only needed for PSX
│                    (e.g. scph5500.bin / scph5501.bin / scph5502.bin)
├─ images/
│  ├─ logos/        <CONSOLE>.png console logos for the Games menu
│  ├─ border/        4:3 "whole‑screen" CRT bezel art (Test Mode)
│  └─ Wireframes/    per‑console placeholder art
├─ videos/          boot splash video(s)
└─ audio/           gamemusic.mp3, static.wav, etc. (built‑in SFX/menu music)
```

**Per‑console notes**

* Every console folder under `main/cores/<CONSOLE>/` needs a libretro core
`.dll` (e.g. `mgba\\\_libretro.dll`, `snes9x\\\_libretro.dll`). The app scans that
folder for any file ending in `\\\_libretro.dll` (or, failing that, any `.dll`)
and loads the first match.
* **PSX** bios file goes in `main/bios/` folder, since PSX cores need real console BIOS dumps
rather than a per‑core scratch folder.
* Save files/states live under `main/roms/<CONSOLE>/saves/profile{1,2,3}/` —
see **Save Profiles** below.
* An optional `main/roms/<CONSOLE>/border/border.png` gives that console a
matching CRT/handheld bezel overlay in‑game (falls back to no border if
missing — this never crashes).
* **DVD** isn't an emulator — it's a video player for movie‑style files/discs
and has no core, BIOS, controller mapping, or save profile of its own.

## 3\. Channel numbering

|Channel|What it is|
|-|-|
|**03**|Games (emulator + DVD deck) — toggle on/off in Settings → System|
|**04**|TV Guide — toggle on/off in Settings → System|
|**05–44**|40 user‑programmable "stations," each independently scheduled|

Only Channel 05 is active out of the box on a fresh install; everything else
starts OFF until you turn it on and give it content in Settings → Channels.
At least one content channel (or Channel 03/04) must always stay enabled — the
app won't let you disable the last one.

You can jump straight to a channel by typing its two‑digit number on the
keyboard's number row or numpad (e.g. `0` `5` → Channel 05). The digits are
buffered for \~2.5 seconds before being applied.

## 4\. Controls

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
|W|Captions on/off|Up|Captions on/off|
|A|Rewind (hold to fast‑rewind)|Left|Rewind (hold for faster)|
|S|Pause / unpause|Down|Pause / unpause|
|D|Fast‑forward (hold for faster)|Right|Fast‑forward (hold for faster)|
|Escape|Quit — Yes/No prompt|A button|Select|
|Enter|Confirm Yes/No|Start / Select|Quit game|

All of the above (except D‑pad/movement keys) can be **remapped** — see
Settings → System → Remote Remapping.

## 5\. The Settings menu

Open/close with **M**. Tabs across the top: **System**, **Channels**, **Games**
(only shown while the Games channel is enabled), **Video**, **Theme**. Kiosk
Mode (see below) hides the Channels tab entirely.

### System tab

|Toggle|What it does|
|-|-|
|Channel 03 (Games)|Enables/disables channel‑surfing to the Games deck|
|Channel 04 (TV Guide)|Enables/disables channel‑surfing to the TV Guide|
|Controls Menu on Start|Shows the Controls help screen automatically on boot|
|Boot on Start|Hides Windows and boots straight to fullscreen (quit the program to see the desktop again)|
|Kiosk Mode|Hides File Explorer access and boots fullscreen — locks kids out of settings that touch the filesystem|

Buttons: **Remote Remapping**, **Reset All Settings**, **Exit Program** (and
**Turn Off Computer**, which appears once the app has taken over as the shell).

**Kiosk Mode** specifically removes: the Channels tab (no re‑scheduling), and
the "Select Libretro Core" row in the Games sub‑menu (no browsing the
filesystem to swap in a different core). If Boot on Start is also on, the only
way out is powering off the computer, since there's no desktop to fall back to.

**Remote Remapping** (System tab → Remote Remapping) lets you rebind every
core action — Nav Up/Down/Left/Right, Ch Up/Down, Volume Up/Down, Select,
Back/Minimize, Mute, Menu — and separately rebind the 10 number‑dial digits,
to any physical key (handy for IR remotes whose buttons send unusual scancodes).
WASD/arrow keys can't be reassigned. Press a key to bind it; a quick double‑tap
of Escape/Back cancels a capture in progress.

### Channels tab

A 5×8 grid of channels 05–44, each cell showing its channel number and a
green/red dot for on/off. Selecting a channel opens its **scheduling
sub‑menu**:

* **Status** — on/off for this channel (locked on if it's the last active
channel — you must enable another before turning this one off).
* **Mode: TV / MUSIC** — TV plays your video library on a schedule; MUSIC
turns the channel into an audio‑reactive **visualizer** station instead.
* **Name** — a custom on‑screen channel name (up to 12 characters), edited
in place.

**TV mode** scheduling options:

|Setting|Choices|Notes|
|-|-|-|
|Scheduling Mode|Random Slots, One Slot, Two Slots, Marathon|Controls how many time‑slots per day get filled and how content repeats|
|Pair Shorts Under|0 (off) – 30 minutes|Auto‑pairs back‑to‑back short episodes under this length into one block|
|Commercials|On/Off|When on, opens the **Commercials \& Extras** sub‑menu|
|Episode Order|Sequential / Random|Per‑show "what plays next" order (hidden in Marathon)|
|Block Length|24‑Hour (Full Day) / 8‑Hour Blocks|Whether content is one continuous pool or split into Morning/Evening/Night folders (hidden in Marathon)|
|Content folder(s)|Full Day, or Morning / Evening / Night|Pick the video files/folders that populate each slot|
|Holiday Schedule Overrides|Halloween, Christmas, Valentine's Day, Thanksgiving, New Year's Eve|Optional swap‑in content for those calendar days|
|Change Current Playing Audio Track|—|Only usable while this channel is the one live on screen; fixes a file that's defaulting to the wrong embedded audio track|
|Clear All|—|Wipes every scheduled file for this channel|

**Commercials \& Extras** sub‑menu: separate file pools for **Commercials**,
**Intros**, and **Outros**, plus a placement choice — **Interrupt show** (cut
in around the half‑hour) or **End of show only**. (intro and outros have not been tested or worked on)

Supported video file types for TV/DVD content: `.mp4 .mkv .avi .mov .wmv .webm .m4v .flv` (plus DVD‑specific `.iso .vob .mpg .mpeg .zip .7z`). A file is
auto‑classified as a "movie" instead of an "episode" once it's over \~80
minutes long, which affects how it's slotted into the schedule.

**MUSIC mode** (visualizer channels): pick a **Visualizer Engine Style** —
Random, or one of PrismShards, LiquidChrome, PulseRings, WarpTunnel,
GlowBraid, EqualizerGrid, ParticleFlow, NeonStarfield — and an **Audio Tracks
folder**. Supported audio types: `.mp3 .wav .ogg .flac .m4a .aac .wma`.

**4:3 Mode / Border** (bottom‑left of the Channels tab, 16:9 displays only):
lets you preview how the app behaves on a real 4:3 CRT without changing your
actual Windows display settings — useful for checking letterboxing and the
console CRT‑bezel borders. Border only shows once 4:3 Mode is on.

### Games tab

Pick a console from the list to open its sub‑menu:

* **Status** — enable/disable this console (the last enabled console is
locked on).
* **Select Libretro Core (.dll)** — browse to a specific core file (hidden in
Kiosk Mode).
* **Controller Mapping** — assign pad buttons for this console.
* **Save Profiles 1 / 2 / 3** — three independent save slots per console;
pick the active one before playing.
* **Change Disc** *(PSX only)* — cycles between detected discs of a
multi‑disc game (filenames need a `(Disc 1)` / `(CD 2)` / `\\\[Disc 2 of 2]`
style marker to be recognized as siblings). (this feature is untested)

Supported consoles:

|Console|Label|Extensions|
|-|-|-|
|GB|Game Boy|.gb, .zip, .7z|
|GBC|Game Boy Color|.gbc, .gb, .zip, .7z|
|GBA|Game Boy Advance|.gba, .zip, .7z|
|NES|Nintendo Entertainment System|.nes, .zip, .7z|
|SNES|Super Nintendo|.smc, .sfc, .zip, .7z|
|GENESIS|Sega Genesis|.md, .gen, .bin, .smd, .zip, .7z|
|N64|Nintendo 64|.n64, .z64, .v64, .zip, .7z|
|PSX|PlayStation|.cue, .bin, .iso, .chd, .pbp, .img, .zip, .7z|
|MAME|MAME Arcade|.zip, .chd, .7z|
|GG|Game Gear|.gg, .zip, .7z|
|NGP|Neo Geo Pocket|.ngp, .ngc, .zip, .7z|
|DVD|DVD Video Interactive|.mp4, .mkv, .iso, .vob, .mpg, .mpeg, .avi, .zip, .7z|

Every console except DVD also has a **Screen Size** option per profile (Full /
75% / 50%) and, on most consoles, an optional TV/handheld **border overlay**
pulled from `roms/<CONSOLE>/border/border.png`.

### Video tab

Five sliders, 0–100, applied as a real‑time image filter over whatever's
playing: **Brightness, Contrast, Color, Sharpness, Tint.**

### Theme tab

* **Menu Transparency** — 0–100%, how see‑through the Settings/Controls
overlays are.
* **Background Color** — hue picker for the menu background.
* **Border/Text Color** — hue picker for the neon accent/border color.
* **Channel Transition** — **Black** (grey flash) or **Static** (TV‑static
flash + noise burst) between channel changes.

## 6\. Logs

Two plain‑text logs are written next to the app so problems can be diagnosed
after the fact:

* `crash\\\_log.txt` — full traceback of any uncaught exception (main thread or
background thread).
* `app\\\_log.txt` — rotating general log (2MB per file, 3 backups kept) for
warnings and notable events short of a crash.

## 7\. Quick start

1. For the Games channel: add a libretro core `.dll` under
`main/cores/<CONSOLE>/`, ROMs under `main/roms/<CONSOLE>/`, and (PSX only)
BIOS dumps under `main/bios/`. (cores/bios/roms are auto detected from their file location but if you have several bios/cores you can pick a specific one in the game menu)
2. Launch the app, press **M** for Settings to turn on channels/consoles and
assign content, and press **C** any time for the in‑app controls
reference.

