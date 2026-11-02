===============================================================================
                         RETRO TV EMULATOR - README
===============================================================================

Retro TV Emulator turns a Windows PC into an old-style television. Your own
video and music files become live TV stations that keep a schedule like real
broadcast TV, with a channel guide, commercials, static between channels and
an optional CRT look. Channel 03 is a game and DVD station that runs retro
console games and plays DVDs.

Contents
  1.  What you need
  2.  Installing and first start
  3.  The channel dial (03, 04 and 05 to 44)
  4.  Controls
  5.  The Main Menu, tab by tab
  6.  Setting up a TV station
  7.  How scheduling works
  8.  Music visualizer stations
  9.  Channel 03: games and DVDs
  10. Picture, sound and captions
  11. Burn-in protection
  12. When something goes wrong (the Static fall-back)
  13. Folder structure
  14. Files the app writes (settings, library, logs)
  15. Updates
  16. Performance modes and the GPU
  17. Sending a problem report
  18. Tips


-------------------------------------------------------------------------------
1. WHAT YOU NEED
-------------------------------------------------------------------------------
  - Windows 10 or 11, 64-bit.
  - Your own media: video files for TV stations, music files for music
    stations, ROMs for games, and DVDs, DVD .iso files or movie files for
    the DVD player.
  - For games: a libretro core (.dll) for each console you want to play, and
    BIOS files for consoles that need them (PlayStation needs one).
  - A keyboard. A game controller and a PC remote are optional and can be
    mapped.

VLC (the video engine) and ffmpeg (used for the scans) are bundled with the
app. You do not need to install them.


-------------------------------------------------------------------------------
2. INSTALLING AND FIRST START
-------------------------------------------------------------------------------
  1. Unzip the app folder anywhere you like, for example your Desktop.
     Keep everything in it together.
  2. Run "Retro TV Emulator.exe".
  3. A loading screen with a progress bar appears while the app starts. On
     start-up it:
       - checks your CPU, memory and graphics cards and picks a performance
         mode (Lite, Standard or Performance),
       - wakes up the drives that hold your media, so the first show does
         not stall,
       - reads your screen size and lays out the menus and the TV Guide for
         a 4:3 or a 16:9 screen,
       - loads the saved TV Guide and schedules,
       - starts the video engine.
  4. The first time, the Controls screen shows the keys. Press C at any time
     to bring it back. You can stop it showing at start-up from the System
     tab ("Controls Menu (Nav Info)").
  5. Press M to open the Main Menu and set up your channels (section 6).

Screen shapes: 4:3, 5:4 and 16:10 screens use the 4:3 layout; 16:9, 21:9
and 32:9 screens use the 16:9 layout.


-------------------------------------------------------------------------------
3. THE CHANNEL DIAL
-------------------------------------------------------------------------------
  Channel 03        Games and DVD player (can be turned off in the System tab)
  Channel 04        TV Guide (can be turned off in the System tab)
  Channels 05-44    Your own stations: 40 channels, each one either a TV
                    station (videos) or a music station (music with a
                    visualizer)

You can turn any channel off to shorten the dial. At least one station
always has to stay on, so you can never turn off the last one.

If you turn off the channel you are watching, the TV tunes to the next
channel that is on.

You can type a channel number with the number keys (two digits, for example
0 then 7 for channel 07). A single digit tunes after a short pause.


-------------------------------------------------------------------------------
4. CONTROLS
-------------------------------------------------------------------------------
Press C to see these on screen. Keys can be changed in
System > Advanced Settings > Remote Remapping.

WATCHING TV
  Up / Down        Channel up / channel down
  Left / Right     Volume down / volume up
  N                Mute / unmute
  M                Open / close the Main Menu
  C                Open / close the Controls screen
  0-9              Type a channel number
  Escape           Minimize the window

TV GUIDE (channel 04)
  W / S            Scroll channels up / down
  A / D            Scroll time slots left / right
  Enter            Tune to the selected channel
  The guide closes by itself after 60 seconds without a key press and goes
  full screen on the channel it was showing.

MENUS
  W / S            Move up / down
  A / D            Move left / right, or change a value
  Enter            Select / confirm
  Escape           Back (goes back to the menu you came from)
  Menus close by themselves after 60 seconds without a key press.

GAME CHANNEL - KEYBOARD
  W / A / S / D    Move through the menus
  Enter            Select / confirm
  Escape           Back / quit the game

GAME CHANNEL - CONTROLLER
  D-pad            Move through the menus
  A button         Select / confirm
  B button         Back
  Start + Select   Hold both for about half a second to quit the game

DVD PLAYER - KEYBOARD
  Play key         Play / pause
  Rewind key       Tap to skip back, hold to seek
  Fast-forward key Tap to skip ahead, hold to seek
  Backspace        Return to the disc's main menu (disc images only)
  Escape           Quit (asks Yes / No)
  Enter            Confirm Yes / No

DVD PLAYER - CONTROLLER
  Down             Play / pause
  Left             Tap to skip back, hold to seek
  Right            Tap to skip ahead, hold to seek
  Y button         Return to the disc's main menu (disc images only)
  A button         Select
  Start + Select   Hold both for about half a second to quit


-------------------------------------------------------------------------------
5. THE MAIN MENU (press M)
-------------------------------------------------------------------------------
The Main Menu has five tabs: System, Channels, Games, Video and Theme.

SYSTEM
  Games (Channel 03)          Turn channel 03 on or off.
  TV Guide (Channel 04)       Turn channel 04 on or off.
  Controls Menu (Nav Info)    Show the Controls screen at start-up.
  Kiosk (Hide Scheduling)     Locks the menus so others can only use the
                              basic options (see below).
  Loudness Scan               Measure each file's loudness so channels play
                              at an even volume.
  Pixel Orbiter               Burn-in protection (section 11).
  CRT TV Bezels               Show a TV frame around the picture.
  Closed Captions             Captions on TV stations (section 10).
  Mono Audio                  Play everything in mono.
  Advanced Settings:
    Look for Updates          Check for a new version (section 15).
    Remote Remapping          Set your own keys for the TV, the menus and a
                              PC remote; also the number keys.
    Performance Mode          Lite, Standard or Performance (section 16).
    GPU/CPU                   Choose which graphics card draws the picture,
                              or the CPU only.
    Custom Shell Launcher     Start the app instead of the Windows desktop,
                              for a dedicated TV or cabinet PC.
    Server Mode               Shown greyed out; not available in this build.
  Reset All Settings          Puts the settings back to defaults. Your files
                              and the scan results in the media library are
                              not touched.
  Exit Program                Close the app.
  Turn Off Computer           Shut down Windows.

  Kiosk Mode hides the scheduling and setup screens and leaves the game
  options, the picture settings and the theme.

CHANNELS
  The grid shows channels 05 to 44. Pick a channel to open its settings
  (section 6). The Channel Settings screen also holds the CRT effects for the
  TV stations: CRT TV Bezels (and the bezel style), Warp Screen, Boot Anim,
  Static Noise, Reception Lines and Scan Lines.

GAMES
  The game and DVD options for channel 03 (section 9).

VIDEO
  Screen mode for the TV stations (channels 05 to 44):
    Normal Mode    Shows the whole picture in its own shape, with bars
                   (letterbox) where it does not fill the screen.
    Stretch Fit    Stretches the picture to fill the screen.
    Crop Fit       Zooms in to fill the screen without stretching; the
                   edges are cut off.
    Pan & Scan     A crop that keeps the action in the middle of the frame.
  Sliders: Brightness, Contrast, Color, Sharpness and Tint. Turning Color all
  the way down gives a black-and-white picture.
  The DVD player and the games have their own screen mode settings in their
  own menus, so these settings do not change them.

THEME
  The menu colour, background colour, menu see-through amount, volume bar
  style, what plays between channels (static or a plain screen), the static's
  colour (white, grey, rainbow or matched to the menu background) and the
  letterbox bar colour (black or grey).


-------------------------------------------------------------------------------
6. SETTING UP A TV STATION
-------------------------------------------------------------------------------
  1. Press M, go to Channels and pick a channel number.
  2. Choose the station type: TV station (videos) or music station (music
     with a visualizer).
  3. Give the channel a name.
  4. Pick the block mode (how the day is split):
       24-Hour    One "Full Day" block that rolls all day.
       8-Hour     Three blocks: Morning (4am-12pm), Evening (12pm-8pm) and
                  Night (8pm-4am). Each block can have its own shows.
       4-Hour     Six blocks: 12-4am, 4-8am, 8-12pm, 12-4pm, 4-8pm, 8-12am.
  5. Add folders or files to each block with the built-in file browser.
  6. Pick the scheduling mode, the episode order and the other options
     (section 7).
  7. Optional: turn on commercials and add a Commercials folder, plus
     Fade-in and Fade-out clips (for example "we will be right back" and
     "we are returning" bumpers). Intros and Outros can be added too.
  8. Optional: holiday schedules (section 7) and a corner watermark logo.

How to lay out your shows
  The app finds shows, seasons and episodes from the folder and file names,
  so keep each show in its own folder, for example:

    TV Shows\
      Show Name\
        Season 1\
          Show Name S01E01.mkv
          Show Name S01E02.mkv
        Season 2\
          Show Name S02E01.mkv

  Season folders named "Season 1", "Season 01" and similar are understood,
  and so are episode names like S01E01 or "Season 1 Episode 1". A video
  longer than 80 minutes is treated as a movie. Movies split into parts
  (Part 1, Part 2 ...) are kept together. Duplicate copies of the same
  episode are only scheduled once.

  Video files: .mp4 .mkv .avi .mov .webm .m4v .wmv .flv .mpg .mpeg .ts .m2ts
  Music files: .mp3 .wav .ogg .flac .m4a .aac .wma
  Caption files next to a video: .srt .vtt .ass .ssa .sub .smi (same name as
  the video, for example "Show S01E01.srt" or "Show S01E01.en.srt").

The first scan
  When you add media, the app scans each file in the background:
    - Duration scan: how long it is, so the TV Guide lines up.
    - Picture box scan: finds black bars burned into the video, so the
      picture can be framed properly.
    - Loudness scan: measures the volume so channels play at an even level
      (only when Loudness Scan is on).
    - A check that the file plays. A file that is truly broken is taken out
      of the schedule, and a short episode it was paired with gets a new
      partner.
  The results are kept in the media library (section 14), so each file is
  only scanned once unless it changes. A message shows on a channel while its
  first files are still being scanned.


-------------------------------------------------------------------------------
7. HOW SCHEDULING WORKS
-------------------------------------------------------------------------------
Every station runs on the clock, like real TV. If you tune in halfway
through a show, you join it halfway through. When the app was closed for a
while, it works out what would have aired in the meantime, so the schedule
and the TV Guide carry on where real time is, not where you left off.

Scheduling mode (how many episodes of a show play before the next show)
  One Slot       One episode, then the next show.
  Two Slots      Two episodes in a row.
  Random Slots   1 to 3 in a row. The show with the most episodes gets 3,
                 and smaller shows get fewer.
  Marathon       Plays a whole show (or a movie series) through before
                 moving on.

Episode order
  Sequential plays episodes in order and remembers where each show is up to.
  Random plays them shuffled; every episode plays once before any repeats.

Pair Shorts Under
  Joins two short episodes into one time slot (for example two 11-minute
  cartoons in a 30-minute slot). You choose the length: 15 minutes by default,
  up to 30, or 0 to turn it off. In random order, when a show has an odd
  number of short episodes, the one left over is the first to play in the
  next round.

Commercials
  With commercials on, breaks are placed either inside the show at the half
  hour (with at least 5 minutes between breaks, at most 2 breaks in a half
  hour) or at the end of each show. A file longer than 5 minutes is never
  treated as a commercial. Fade-in and Fade-out clips play around the breaks.

Changing the settings
  A change to the scheduling settings takes effect from what is still ahead
  in the schedule. What already aired stays as it was.

Holiday schedules
  Each channel can have Halloween, Christmas and Thanksgiving schedules. A
  holiday schedule plays for the 3 days up to and including the holiday
  (Thanksgiving is the fourth Thursday of November), always in random order.
  A holiday can also be forced on by hand.

Audio track memory
  If you switch a show's audio track while it plays (for example to another
  language or the commentary), the app remembers your choice and uses that
  track every time the show airs.


-------------------------------------------------------------------------------
8. MUSIC VISUALIZER STATIONS
-------------------------------------------------------------------------------
A music station plays your music on the clock like a TV station, with a
moving picture drawn to the music.

  - Styles: Prism Shards, Liquid Chrome, Pulse Rings, Warp Tunnel, Glow
    Braid, Equalizer Grid, Particle Flow, Neon Starfield and Lightning.
    Random changes the style with each song; you can also lock a channel to
    one style.
  - Reactive Visualizer: in Standard and Performance mode the picture follows
    the real music (the beat, the bass, the mids and the highs). Each song is
    analysed once in the background and the result is saved. A strength
    slider sets how strongly the picture reacts. In Lite mode the picture
    moves on its own, without following the music.
  - The song's name shows in the top-left corner and goes away after 60
    seconds (burn-in protection).
  - Music stations have their own volume adjustment.


-------------------------------------------------------------------------------
9. CHANNEL 03: GAMES AND DVDS
-------------------------------------------------------------------------------
Turn channel 03 on in the System tab. Then turn on the consoles you want in
the Games tab. Only the DVD player is on at first.

Consoles
  Game Boy (GB), Game Boy Color (GBC), Game Boy Advance (GBA), NES, Super
  Nintendo (SNES), Sega Genesis, Sega 32X, Sega Master System, Game Gear,
  Nintendo 64, PlayStation (PSX), Atari 2600, Neo Geo Pocket and Neo Geo
  Pocket Color, plus the DVD player.

What each console needs
  - A core: a libretro core .dll in main\cores\<CONSOLE>\ (for example
    main\cores\SNES\snes9x_libretro.dll). If a console has several cores,
    you can pick one; otherwise the app uses the best one it finds.
  - ROMs: in main\roms\<CONSOLE>\, or found for you by "Scan for ROMs".
    ROMs inside .zip, .7z or .rar files work too.
  - BIOS (PlayStation): in main\cores\PSX\system\. Use "Add BIOS" in the
    console's menu to pick the file. When there are several, a USA BIOS is
    picked first.

ROM file types
  GB .gb  GBC .gbc .gb  GBA .gba  NES .nes  SNES .smc .sfc
  Genesis .md .gen .bin .smd  32X .32x .bin .smd .md  Master System .sms
  Game Gear .gg  N64 .n64 .z64 .v64  PSX .cue .bin .iso .chd .pbp .img
  Atari 2600 .a26 .bin  Neo Geo Pocket .ngp  Neo Geo Pocket Color .ngc
  All of them also inside .zip, .7z or .rar. Opening .7z and .rar needs
  7-Zip installed.

Games tab options
  Scan for ROMs / Scan for Cores, Clear ROMs / Clear Cores, controller
  mapping, stick deadzone, multitap (players 3 and 4), the console scroll
  style (carousel, side to side or up and down), the list text colour, the
  game screen mode, and a background picture for the game menu.

Per-console options
  Core choice, controller mapping, splash screen on or off, the splash image
  and video, Add BIOS, the ROM folder location, and clearing that console's
  ROM list.

Splash screens
  When the splash screen is on but you have not picked an image, the app
  uses <CONSOLE>SplashScreen.png from that console's folder in
  main\images\splash screen images\ (for example GBASplashScreen.png or
  PSXSplashScreen.png). An image you pick is copied into that folder and
  used whatever its name is.

Saves
  In-game saves and save states are kept per game in
  main\roms\<CONSOLE>\saves\profile1 (2 and 3 are the other save profiles).
  A save is written to a temporary file first, so a crash or power cut never
  leaves a half-written save.

Controllers
  XInput and DirectInput controllers work. Each player can be given a
  controller, and buttons can be mapped per console.

DVD player
  - Plays DVDs from your disc drive, DVD .iso files, and movie files (.mp4,
    .mkv, .vob, .mpg, .avi ...) from main\roms\DVD\. Encrypted DVDs are
    supported.
  - Disc menus work (VLC handles the menu highlights). Movie DVDs only;
    interactive DVD games are not supported.
  - Options: captions, resume (starts where you stopped), button mapping,
    screen mode and audio track.

The channel 03 screen saver starts when the game menu is left idle.


-------------------------------------------------------------------------------
10. PICTURE, SOUND AND CAPTIONS
-------------------------------------------------------------------------------
CRT look (Channels > Channel Settings)
  CRT TV Bezels (a TV frame, with several styles), Warp Screen (curved
  glass), Scan Lines, Static Noise, Reception Lines and the Boot animation.

Loudness
  With Loudness Scan on, every file is measured and played at the same level
  (the broadcast standard of -24 LUFS), so channel changes have no big volume
  jumps. Smart Loudness learns from your changes: if you set the volume of
  files in a series by hand, it applies the same change to the rest of that
  series. It also learns from how you use the volume slider while a show
  plays.

Captions
  The app uses a caption file next to the video, or captions inside the file,
  when there are any. When there are none, it can make captions live from
  the speech (this needs the speech model in main\models\whisper). TV
  stations and the DVD player have separate caption switches. Made captions
  are saved in main\captions so they are only made once.

Mono Audio (System tab) mixes everything to mono.


-------------------------------------------------------------------------------
11. BURN-IN PROTECTION
-------------------------------------------------------------------------------
  - Pixel Orbiter: moves the picture slightly over time so nothing stays on
    the same pixels.
  - Menus and the Controls screen close by themselves after 60 seconds idle.
  - Song names on music stations go away after 60 seconds.
  - Channel 03 screen saver.
  - Static is used whenever something goes wrong (section 12).

The app warns you before you turn off a protection: turning off the Pixel
Orbiter, leaving the bezels on, locking a music station to one visualizer
style, or turning off the channel 03 screen saver. Each warning has a
"Don't show this again" box.


-------------------------------------------------------------------------------
12. WHEN SOMETHING GOES WRONG (THE STATIC FALL-BACK)
-------------------------------------------------------------------------------
On a TV or music station, anything that goes wrong falls back to Static: a
file that will not play, a missing file, a drive that has failed or stopped
answering, or nothing scheduled. A banner explains why for 30 seconds and
then goes away. The station stays on Static while it keeps trying the file
again, and the next show starts on time.


-------------------------------------------------------------------------------
13. FOLDER STRUCTURE
-------------------------------------------------------------------------------
Your media (TV shows, movies, music, commercials) can be anywhere, on any
drive. You point the channels at it. The app's own folders are:

Retro TV Emulator\                      the app folder
  Retro TV Emulator.exe                 the app
  retro_config.json                     your settings and channels
  .retro_tv_device_id                   this PC's id (do not edit)
  version.json                          the installed version, written by
                                        updates (hidden; do not edit)
  logs\                                 everything the app makes while it
                                        runs (section 14):
    temp\                               the logs and the tester report
    controllers\                        your controller setup
                                        (emulator_config.json)
    watchdog\                           the relaunch watchdog's record
    .retro_tv_updates\                  update downloads, while an update
                                        installs
    playback_runtime.json               where each station is up to
    update_helper.log                   written while an update installs
  _internal\                            the app's own files; do not move or
                                        rename anything in here
    vlc\                                the bundled video engine
    ffmpeg.exe                          the bundled scan tool
    main\                               the app's data folder:
      cores\
        <CONSOLE>\                      libretro cores (.dll) for that
                                        console
          system\                       that console's BIOS files
      roms\
        <CONSOLE>\                      that console's ROMs
          saves\profile1 .. profile3\   in-game saves and save states
        DVD\                            movie files and DVD .iso files
      images\
        bezel\                          TV bezel pictures (bezel.png,
                                        bezel1.png, bezel2.png ... one per
                                        bezel style)
        splash screen images\
          <CONSOLE>\                    that console's splash image, for
                                        example GBA\GBASplashScreen.png
          MENU\                         the game menu background picture
          WATERMARK\                    channel watermark logos
        logos\                          console logos for the game menu
        loading\                        the loading screen art and the
                                        app icon (icon.png)
        screensaver\                    ScreenSaver.png for the channel 03
                                        screen saver
        Wireframes\                     controller pictures for the mapping
                                        screen (<CONSOLE>_wireframe.png)
      videos\
        splash screen videos\<CONSOLE>\ that console's splash video
      audio\
        System\                         app sounds (static.wav)
        Game Music\                     music for the game menu
                                        (gamemusic.mp3)
      media_library\                    library.sqlite3, the scan results
                                        (section 14)
      loudness\                         loudness scan working files
      captions\                         captions made from speech
      models\whisper\                   the speech model used for captions

<CONSOLE> is one of: GB, GBC, GBA, NES, SNES, GENESIS, 32X, MASTERSYSTEM,
GG, N64, PSX, A2600, NGP, NGPC, DVD.


-------------------------------------------------------------------------------
14. FILES THE APP WRITES
-------------------------------------------------------------------------------
retro_config.json (in the app folder)
  All settings and every channel's setup and schedule position. Back it up
  before big changes. "Reset All Settings" resets this file only.

main\media_library\library.sqlite3
  What the scans found for each file: its length, loudness, picture box,
  captions and whether it plays. Files are recognised by their content, so a
  renamed or moved file keeps its results. Reset All Settings does not touch
  it.

logs\temp\ (in the app folder)
  app_log.txt               the main log of everything the app does
  crash_log.txt             written if the app crashes (empty means no
                            crash)
  crash_log_previous.txt    the crash log from the run before
  vlc-debug.log             the video engine's own log
  vlc-debug_previous.log    the same from the run before
  tester_report.txt         a summary written at start-up and again when
                            the app closes: build and PC facts, whether the
                            last run ended badly, the crash logs, and the
                            latest warnings and errors


-------------------------------------------------------------------------------
15. UPDATES
-------------------------------------------------------------------------------
The app checks for a new version at start-up and asks before it installs.
You can also check by hand with System > Advanced Settings > Look for
Updates. Ticking "Don't show this again" stops the start-up question; a
check by hand still asks. The update installs itself, keeps your settings
and data, and restarts the app.


-------------------------------------------------------------------------------
16. PERFORMANCE MODES AND THE GPU
-------------------------------------------------------------------------------
  Lite          For slower PCs. Fewer background jobs at once; the music
                visualizer moves on its own instead of following the music.
  Standard      The normal mode.
  Performance   For fast PCs. More background work at once, the full
                24-band reactive visualizer, and faster scans.

The app picks a mode for your PC at start-up. You can change it in Advanced
Settings, but a PC the start-up check rates as a slow PC cannot be put in
Performance mode. While a show plays, the app holds back its background work
whenever the picture or sound starts to struggle.

GPU/CPU (Advanced Settings) picks the graphics card that draws the picture,
for example a laptop's dedicated card. If the GPU has problems, the app
switches back to drawing on the CPU by itself.


-------------------------------------------------------------------------------
17. SENDING A PROBLEM REPORT
-------------------------------------------------------------------------------
Close the app normally, then send everything in the logs\temp\ folder:
app_log.txt, crash_log.txt, crash_log_previous.txt, vlc-debug.log,
vlc-debug_previous.log and tester_report.txt. Add retro_config.json, and logs\update_helper.log if the problem is about
settings or an update. Say what you
did and roughly what time it happened, so the right part of the log can be
found.


-------------------------------------------------------------------------------
18. TIPS
-------------------------------------------------------------------------------
  - Keep each show in its own folder with season folders; the schedule and
    the guide are only as good as the names.
  - Let the first scans finish before judging the volume or the framing.
  - External drives that sleep can delay the first show; the app wakes them
    at start-up and falls back to Static (and retries) if one stops
    answering.
  - Channel 04 (the TV Guide) shows what aired, what is on and what is next
    on every station.
  - Use the three save profiles on channel 03 to keep separate game saves
    for different players.

===============================================================================
