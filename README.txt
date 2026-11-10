Tutorial
Start Here - Watch or Read Below
What You Need
Retro TV Emulator will turn your own downloaded video and music library into scheduled TV Stations and Music Visualizer Stations on a Windows PC. Channel 03 adds retro games front end and a DVD/media player (need optical drive for DVDs), Channel 04 is a TV Guide, and there are plenty of theme and static options to have fun with.
Computer: A 64-bit Windows 10 or 11 PC. The app checks your hardware at every start and adjusts itself to run its best on any PC that can run windows 10 or 11. Linux and Mac can run Windows programs through compatibility tools; how well depends on the tool and your setup but you can run this on Linux and Mac with those tools.
Keyboard, mouse and controller: A keyboard for the TV and menus controls, a mouse for the file explorer controls, and a compatible controller for playing games (launched games only use a controller). You can also remap a PC remote to control channel changing and menu navigation.
Your media: You will need your own videos, music and commercials (including commercial bumpers). They can stay on any drive, internal or external (for app portability put the app on an external drive with the files you want to travel with it); the app will open a file explorer and you will point to the location of your media. The app will remember and pull from those locations. You can also create shortcuts to media so you can organize media per channel without duplicate files.
For games: You will need your own ROMs, BIOS files and libretro emulator Cores. For now i just point my app at the System, Core, and Roms folders in my Retroarch folder. DVDs work from an optical drive or as .iso backups. You can use the same DVD player as a media player for any video file.
Included tools: Everything you need to run this app is here. No side downloads, installs, or coding needed.
Download & Install
Download the app from the Download page, unzip the app folder somewhere such as your Desktop, Program Files or portable storage drive for a portable build, and run "Retro TV Emulator.exe". Create a shortcut for desktop or pin it to your taskbar or start if you would like. Do not move, rename, or delete anything inside _internal.
Your settings live beside the exe: The app saves your channels, settings and schedule positions in retro_config.json next to the exe. Keep that file with the app; if it is missing, the app starts as if it had never been used.
Your media stays put: Shows, movies, music and commercials can stay where they are on your computer. You will point the app at their location. Nothing is copied into the install folder. (For portable builds, make sure all media and files are on the same external drive. The project has folders for you to use but you dont have to)
Organize Your Media & Supported Files
Before adding media, keep each show in its own folder with season folders inside it. Clear folder and file names help the app recognize shows and fill in the guide. There are things in place that hide (dvdrip) type tags, but the clearer your naming and sorting, the better it will run.
```
TV Shows
  Show Name
    Season 1
      Show Name S01E01.mkv
      Show Name S01E02.mkv
    Season 2
      Show Name S02E01.mkv
```
```
Movies
  Comedy
    Movie Name.mp4
    Movie Name.mp4
    Movie Name
      1. Movie Name The Rising.mp4
      2. Movie Name The Falling.mp4
```
Shows and movies: Season 1 and Season 01 folders, and S01E01 or Season 1 Episode 1 file names, are recognized. Videos in folders tagged with "Movies" will be treated like movies. Movie parts stay together but need part 1/part 2 tags, and it will reject duplicate names with different file types. The names displayed in the TV Guide will drop season number and other extra text that is not a part of the title.
Caption files: Place your captions file beside the video with the same name, such as Show S01E01.srt or Show S01E01.en.srt. The name must match or it will not grab it. (If you do not have caption files, Live Captions is available for computers with enough memory to run it).
Video formats: MP4, MKV, AVI, MOV, WEBM, M4V, WMV, FLV, MPG, MPEG, TS and M2TS.
Music formats: MP3, WAV, OGG, FLAC, M4A, AAC and WMA.
Caption formats: SRT, VTT, ASS, SSA, SUB and SMI files.
Media Scans and The HUB
When you add media, the app scans each file once and saves the results in the HUB, so the TV Guide, the TV Stations and all cached information all get the same answer and know how to play each file.
What is scanned: Each file’s length is scanned first for scheduling times, it then gets a picture box result for aspect ratio. Next it will get a loudness scan for a gain number for equalizing volume across media. The scan order prioritizes shows in the order they play with the station you’re on first.
Tuning to a channel: The channel you tune to jumps the queue: its show is scanned first so it plays at the right size and volume. (this only matters if the show is unscanned)
Saved results: The scan results are saved and stored for the next time the video plays. If you clear a station, you can add the media back and it will not need rescanned. If you Clear All Settings in System menu, it does clear the saved scan data.
Booting Up
Each start shows a loading screen while the app gets everything ready. It will build the proper cache files so the first channel plays straight away. It will find your computers hardware specs to adapt to your computer. The first time you boot the program has no channels yet, so every station shows static until you add media.
Drives: Internal and external media drives are woken up so a sleeping drive does not delay the first show.
Hardware check: The app counts your CPU cores, threads, and memory. It finds your graphics cards and picks the best one. With this information it will pick your performance mode (Lite, Standard or Performance). It will throttle and adapt as needed to keep things running smoothly.
	LITE	STANDARD	PERFORMANCE
Menu and list speed	30 FPS	60 FPS	60 FPS
Warp Screen and Scan Lines	Greyed out	Available	Available
Background scans	Slower	Normal	Faster
Live captions	Slower	Normal	Faster
Music visualizer	Moves, does not follow the beat	Follows the music	Follows the music with full effects
Graphics card	Not needed	Not needed	Required
Saved information: Show lengths, loudness, picture framing and the TV Guide schedule are loaded from the app’s media library, so channels open without waiting for scans or starts scans during boot for shows that may not have been scanned for a clean boot.
Windows Screen Resolution: It scans windows screen resolution for a canvas size for proper letter-boxing across devices.
Real-TV clock: Stations run on the clock like real TV. The program will find your computers clock on boot.
Boot Animation option: If Boot Anim is turned on, the old-TV power-on animation plays as the app starts.
The Controls
Controls Menu
The Controls Menu shows all the controls across the project and what each one does. It is on by default and shows up when you boot the program so you are greeted with a way to understand how to use the program. This can be turned off in the menu and at any time you can use the C key to bring it back up or close it.
TV Controls: The Up/Down arrow keys will change channels. The Left/Right arrow keys will change the volume, N mutes/unmute, M opens/closes the Main Menu, C opens/closes the Controls Menu. To minimize or full screen the program, hit Escape then Enter. If you want to go straight to a channel you can use the number keys to type it in.
TV Guide: The W/S keys will move up and down between channels loading a preview window to view what is on each station, A/D move through the time slots moving left and right to see what is on later, Enter will take you to the highlighted channel.
Main Menu: The W/S keys move up and down, A/D move sideways or change a value that adjusts up and down, Enter selects, Escape goes back from sub menus or quits menu. The file explorer is used with the mouse only.
Games: Keyboard (W/A/S/D, Enter, Escape (to quit)) for the game menus navigation, and the controller uses (D-pad, A, B, Start + Select (to quit)). Launched games are played with a controller only.
DVD Player: The DVD player’s keyboard keys and controller buttons are the same as the games for navigation of menus, select and quit but when menus are not open the controller left, down, and right are for rewind, pause/unpause, and fast-forward. The keyboard controls for this are keys with those symbols. Backspace or Y when playing a real DVD or DVD backup will take you back to the main menu.
System Tab
System Menu
The Main Menu opens on the System tab.
Games (Channel 03): Turns the game and DVD channel on or off. When it is off, Channel 03 is skipped while channel surfing and the Games tab is hidden.
TV Guide (Channel 04): Turns the TV Guide channel on or off.
Controls Menu: The Controls Menu toggle decides if the app shows the Controls Menu when the program boots.
Kiosk: This will lock the app for everyday viewing once it is set up: the scheduling and setup options are hidden so they can not be changed.
Loudness Scan: This turns off if you are using the saved gain number that helps equalize audio from show to show, channel to channel. Turning this off does not stop scans.
Pixel Orbiter: This option moves the on-screen picture and text very slightly over time so nothing sits on the same pixels, to help prevent burn-in. Turning it off shows a warning about burn in.
Closed Captions: This shows captions on the TV stations. The small box beside the switch sets the caption color. The DVD player has its own captions switch. This option is not available in Lite Mode.
Mono Audio: This mixes the left and right sound into one, for a single speaker or one ear. This is for TV's with only one speaker.
Reset All Settings: Puts every setting back to its default and clears the saved scan results and TV Guide data after asking you to confirm. Your media files are not deleted even if moved to the app folders.
Exit Program: This closes the app from the menu.
System Menu - Advanced Settings
Advanced Settings from the System:
Look for Updates: Every time you boot it will show you if there are updates. If you keep it running or turned off update notifications you can check for a newer version here.
Remote Remapping: Changes which key controls what. This is where you would change controls to work on a PC remote.
Performance Mode: On boot a performance mode is chosen for you but you can change it. Lite has menus at 30 fps, greyed out options that wont work, and slower scans. Standard is all normal scans and access to anything that is not performance. Performance unlocks faster scanning, more games, and higher fps. You can change this manually but if you are booted as a Lite computer, you will not have a performance mode.
GPU/CPU: The best options is chosen on boot but you can change the GPU at any time, even change to CPU only. Performance mode has to use a GPU though so any CPU only computer also will not have a performance mode.
Custom Shell Launcher: This option will start the emulator in place of the Windows desktop so Windows can no longer be accessed. This option combined with Kiosk mode turns the computer into a cable box instead of a computer.
Advanced Settings - Remote Remapping
The Remote Remapping will show every action and the key it uses now. Remap Remote goes through the actions one by one. Press the key or PC remote button you want for each. Remap Numbers does the same for the number keys 0 to 9, these are here for any keyboard or remote not registering the number keys.
Advanced Settings - Server Menu
Server Mode lets your computer share its channels schedule over your wifi with other computers on that network. All they need is a copy of the program on their device and to scan the network for servers. Turn on the Server option and open its menu.
Server: Click on Server to select/name/edit/reset a server profile. Your stations will be reset (this will not affect your current scheduling, just replace it while editing), and you can set up your server scheduling. When done you can scan for the server and stream those stations and their scheduled media.
User: Click on User to select a Server or leave server mode. Scan for servers and pick one, or leave the menu to go back to your regular scheduling. If you pick a server to stream, select Clear User Choice to remove that server choice. While using a server you can still use your own media on channels not set up on that server.
Channels Tab
Channels Menu
Set up your stations here. You have 40 channels over two pages to choose from and schedule.
Channel buttons: Channels 05–44, twenty per page. A green dot means the channel is on and a red dot means it is off. Select a channel to open its settings. Once media is added each one of these channel buttons has its own loading bar to show the progress of media scans.
1/2 Arrow button: This button reveals the other page of channels.
Channels Menu - Settings
The settings sub menu in the Channels Menu holds options for Bezels and other filters.
CRT TV Bezels: Frames the picture inside a vintage TV set. There are 3 options.
Warp Screen: Curves the picture and darkens its corners like the glass of a CRT. This option is only for the bezel and does not work if a bezel is not on. This option is not available in Lite mode.
Boot Anim: This option plays an old CRT TV power-on animation when the app starts.
Static Noise: This option is a fast-moving see-through snow filter over the picture. It mimics bad reception.
Reception Lines: Bands of interference that roll down the picture now and then. It mimics bad reception.
Scan Lines: This option is thin horizontal lines across the picture like a CRT TV. This is not available in Lite mode.
Channel Menu - Scheduling Settings
Scheduling Settings Menu is where for each channel you tell that channel how to manage your media:
On/Off: Turns the channel on or off. The last channel left on cannot be turned off, and turning off the channel you are watching tunes to the next one.
Music: Makes the channel a Music Visualizer Station with a visualizer instead of video.
Name: Name your channel. Up to 12 letters, numbers or spaces, shown on the TV Guide. After typing in your name, Enter will save it.
Number of Episodes: This option will tell the program how many episodes of one series play in a row before another show: One, Two, or Random (Random is one to three episodes depending on the show’s episode count. The more there is, the more in a row you get). Marathon plays through one whole series or movie collection before moving to the next.
Episode Order: In Order follows the episode numbers until all episodes have played and then repeat. Random plays every episode at random once and then repeats.
Pair Shorts Under: This option lets you set an episode length up to 30 mins. Any episodes shorter than the set time, gets combined into one episode. You can turn this off by setting the time to 0.
Schedule Length: This option allows you to set how long each scheduled block runs for. 24-Hour uses one Full Day block, this is the easiest to schedule but has the least control over what comes on. The 8-Hour blocks uses Morning (4am–12pm), Evening (12pm–8pm) and Night (8pm–4am). The 4-Hour uses six blocks: midnight–4am, 4–8am, 8am–noon, noon–4pm, 4–8pm and 8pm–midnight. This one gives you the most control over your daily schedule.
Block buttons: These buttons are to add media to each block. Each opens the file explorer, where you pick the media you want added to that block.
Clear Media: Removes all of this channel’s saved media from its schedule for this TV Station.
Scheduling Settings - Commercials
Turn on Commercials for Commercial breaks. Any video over 5 mins will be rejected as not a commercial.
Commercials: The commercial clips played in each break. Advertising. The more you add the better it works or you will get a lot of repeats.
Fade-In: A clip played at the start of every show, such as "Thanks for sticking with us." or "Welcome back".
Fade-Out: A clip played when a show ends, such as “we’ll be right back” or “thanks for watching”.
Mode: The Interrupt Show option puts breaks inside a show at the half-hour marks or half way point for shorter shows. The End of Show plays them only after a show has ended.
Clear Media: Removes this channel’s commercials, fade-in, and fade-out media.
Scheduling Settings - Audio Settings
Current Playing Audio Settings works on the channel you are watching (you have to be tuned to the channel you’re editing).
Audio track: Pick a track with Enter; the channel remembers it for that show.
Loudness gain: A and D make the show louder or quieter in 1 dB steps, from −12 to +12. Your change also teaches Smart Loudness, which uses it for the series’ other episodes.
Scheduling Settings - Holiday Schedule
Each channel can have its own Halloween, Christmas and Thanksgiving media. In the channel’s settings open Holiday Schedule; each holiday button opens the file explorer for that holiday’s shows.
When they play: A holiday schedule takes over for the three days up to and including the holiday.
Force: Plays that holiday’s schedule now, whatever the date, until you turn it off. It also shows that holiday’s bezel. You can easily extend the 3 day holiday run by turning it on early.
Clear Media: Removes this channel’s holiday media.
Scheduling Settings - Watermark
When you turn on the Watermark, the program has a default watermark. Click the add button to add your own. Each channel can have its own Watermark. You can find old TV logos or make your own. The image you choose will be added to the folder so you can switch between watermarks later.
Scheduling Settings - Visualizer
If you turn on the Music toggle in a channel’s settings to make it a music station. Music plays on the clock like a TV station, with a visualizer on screen instead of video.
Name: Name your channel. Up to 12 letters, numbers or spaces, shown on the TV Guide. After typing in your name, Enter will save it.
Visualizer Engine Style: Pick the Visualizer you would like or let it play Random Visualizers. Random is a new randomly selected Visualizer each song. This setting helps prevent burn in.
Audio Files: Add audio files to the file explorer to pick the station’s music.
Loudness Gain: Makes the playing song louder or quieter. It works only while this station is on screen and a song is playing; otherwise the row is greyed out.
Reactive Visualizer: Makes the visualizer follow the beat, bass, mids and highs. Each song is analyzed once and saved. In Lite mode the visualizer moves without following the music.
Reactive Strength: How strongly the visualizer reacts to the music, from subtle to strong.
Clear Media: Removes this station’s music from its schedule.
On screen: The song’s title shows in the top-left corner and disappears after 60 seconds.
Video Tab
Video Settings
The Video Settings allow you to adjust how your video looks. Each slider runs from 0 to 100 with 50 as normal.
Brightness: Lifts or darkens the whole picture.
Contrast: Widens or narrows the gap between dark and bright.
Color: How strong the colors are; all the way down gives a black-and-white broadcast.
Sharpness: Sharpens or softens edges.
Tint: Shifts the colors toward green or purple, like the tint knob on an old set.
Screen Modes
The Screen Modes decide how each show fills your screen. They apply to Channels 05–44. The games and the DVD player both have their own.
Normal Aspect Ratio Mode: Keeps the picture’s shape, adding black bars to the top and bottom or both sides to fit the video properly to your screen size (if the video size fits the screen size no black bars will be present).
Stretch to Fit: This option removes the black bars and stretches the picture to fill the screen.
Crop to Fit: This option zooms in the video, cutting off part of the picture to get it to fill the screen without stretching the image.
Pan & Scan: This option is the same as Crop to Fit but it pans to the cut off sections to keep action on the screen.
Theme Tab
Theme Settings
The Theme Settings let you customize how things look.
Volume Bar: You can choose between two different volume bar styles.
Channel Static: This changes the color of the static. White (standard), Grey (shows up lighter), Rainbow (colored static shows up heavier), or Theme that matches your theme color choice (colored static shows up heavier). It changes the color of all static from channel static to static filters.
Channel Transition: This option is a choice between static between channels or a quick black screen.
Letterboxing: The color of the bars on the sides or top and bottom of videos can either be black or TV grey.
Menu Opacity: This option decides how see-through the menus are, so the show behind stays visible if you need it to.
Background Color: This option changes the color of the menu backgrounds.
Border/Text Color: This option changes the color of the borders, highlights and titles.
TV Guide
The TV Guide looks at the HUB and keeps the saved scheduled media up to date for you to see what comes on and when.
Moving around: W and S move between channels, A and D move through the time slots, and Enter tunes to the highlighted station.
Checking your schedule: After building a station, look here to see what aired, what is on and what comes next.
Preview: The preview shows what is playing on the highlighted station.
Time-out: If you stay idle on the TV Guide, after 60 seconds it will take you to the station you were previewing to prevent burn-in.
Games Tab
Games Settings
Each Logo is a button to a game console and its options:
Arrow button: Shows more game consoles.
All Consoles: Turns every console on or off at once except the DVD Player.
All Settings: If you want to turn on settings across all consoles all at once like auto save and auto load or splash screens, turn this setting on.
Screen Saver: This will start a moving screen saver when channel 03 sits idle longer than the decided time. It is burn in protection for channel 03.
Screen Saver Timer: How long the menus wait before the screen saver starts.
Game Music: Plays music while you browse the game menus.
Set Music: Picks your own tracks for the game menus.
Games Menu - DVD & Media Player
Choose DVD on Channel 03 to play DVDs from your optical drive, DVD .iso backups, or plain video files. DVD menu is in the Games Tab.
Button Mapping: Changes which keys control the DVD/Media Player.
Screen Mode: Normal Aspect Ratio, Stretch to Fit, Crop to Fit or Pan & Scan.
Change Audio Selection: Picks the movie’s audio track while media is playing.
Splash Screen: Your own picture or video while a disc loads. If it is turned on but nothing added it uses the default splash screen.
Captions: Uses saved captions next to files with the same name. If files don’t exist it uses live captions. If your computer does not have enough memory to do this, captions are turned off.
Resume: Starts media again where you stopped it.
Games Settings - Games Menu
Menu Options holds the settings shared by every console:
Controller Mapping: The Menu mapping is not for game play. It is for controlling channel 03 and the DVD player. Each console will have its own mapping option.
Map Controller: Goes through the buttons one by one: press the button you want for each one.
L-Stick as D-Pad: Lets the left analogue stick move around the menus like the D-pad.
Pick Controller: Chooses which connected controller you are mapping; the number beside it is the player.
Close: Returns to the Games menu.
Stick Deadzone: How far the analogue stick must move before it counts, so a worn stick does not drift.
Background Image: A picture behind the game menus.
Multitap (All Consoles): Adds players three and four on consoles that support them.
Game Screen Mode: Normal Aspect Ratio Mode keeps the game original size; Stretch to Fit fills the screen. The other screen modes are not included so game menus always stay on screen.
Scroll Style: How the consoles move on Channel 03: Carousel, Side to Side or Up / Down. The color box beside it sets the menu text color.
Scan for Cores and Scan for ROMs: Point them at your RetroArch folder or your retro collection; the app finds and sorts the emulator cores, ROMs and BIOS files for you.
Clear Cores, Clear BIOS and Clear ROMs: Forget the app’s core, BIOS and ROM lists. Clearing cores and BIOS also deletes the copies the app made in main/cores; your own files elsewhere are never deleted.
Console and Profile Settings
Select the console you want to edit. You can add splash screens, change core/bios files, change save profiles, map controllers and turn on bezel options.
Select Libretro Core (.dll): After adding cores in the MENU options, you can switch them in the consoles menu.
Controller Mapping: Each console provides a mapping option.
Save Profiles: Profile 1, 2 or 3 keeps different players’ saves apart; the active one is marked. Each profile has bezel options and save options.
Splash Screen: Your own picture or video while a disc loads. If it is turned on but nothing added it uses the default splash screen.
Auto-Save and Auto-Load: Auto Save writes a save state when you quit the game, leave Channel 03 or the screen saver starts. Auto Load loads that state when the game starts.
Screen Size and TV Bezel: Screen Size allows you to change the size of the screen between 100%, 75%, and 50%. These settings work well with handheld consoles so the image is not stretched big. With bezels on, it works out really well.
Channel 03 Menus
Tune to Channel 03 to see the console picker. It shows the consoles you turned on, plus the DVD player. Select a console to see its games. The highlighted game can be selected to launch the game. The arrows to the right of the roms list will cycle through console lists.
Playing Games
While a game starts, this loading screen shows a progress bar.
Playing: Launched games are played with a controller only.
Pause: The game will auto pause if you leave channel 03 or open a menu.
Quitting: Use start/select or ESC to pull up the yes/no quit menu.
Extras
TV and Game Bezels
A bezel for TV stations and game consoles is the frame of vintage TV sets and handheld consoles drawn around the picture. Turn on CRT TV Bezels in Channels > Settings and the stations play inside the screen of the TV set. This bezel is for the whole program. There are matching bezels you can turn on for each game console in each console’s sub menu, but it’s only for Channel 03 when that console launches. Game handheld consoles each have their own custom bezels that match their handheld’s screen.
Bezel Style: There are 3 CRT TV bezels. The first one matches the boot loading image. The second style is a zoomed in version of the first one for better viewing. The third one is an older TV set.
Warp Screen: Warping the screen makes the bezels take on a real CRT TV feel, warping the image, menus, and other on screen UI. This option only works if the bezels are on.
Burn-in: A fixed frame that never moves adds burn-in risk on long sessions, so turning a bezel on shows a warning.
Console bezels: Each console sub menu has a bezel it can use while that console is launched. All game consoles use the CRT TV bezels but each handheld console has a bezel that matches the screen of that handheld.
Holiday Bezels
Halloween
Thanksgiving
Christmas
Each bezel style can have a Halloween, Christmas and Thanksgiving version. During a holiday the app swaps the bezel for that style’s holiday 3 days in advance of the holiday, and puts the normal one back when the holiday is over. You can also force on a channel’s Holiday Scheduling either for testing or for longer holiday runs (more than 3 days).
Captions
Captions use subtitles inside the video or a caption file beside it. When a video has neither, live speech captions will produce text for you instead. This option is limited to your memory, if you do not have enough memory to use it, the program will grey out that option.
When a Channel Can’t Play
A channel with no media shows moving static. When the program boots for the first time with no media it starts with a static screen. If a station’s media cannot load, a drive is missing or stalls, or a file is broken, the station shows the same static with a short red banner explaining why. The banner goes away after a while to protect the screen, and the app keeps retrying in the background. Check that the files still exist and external drives are connected and awake.
Updates
The app checks for a new version when it boots and asks before installing. You can check a box that will stop these notifications on boot. There is a manual Update button in Advanced Settings that you can use to check if you have notifications off. If you manually check Updates, you can uncheck the box at any time and get notifications again. If you accept the update, a load bar will start and when done, it will restart the app with updates installed.
Burn-In Safeguards
Every menu, pop up, or channel has some kind of burn in protection. Menus close after 60 seconds idle, the Pixel Orbiter moves on-screen elements, the game menu screen saver starts when idle for the set time in menu, the TV Guide will take you to the previewed station when idle, all channels fall back to static, the app reboots if it crashes (only helps burn in, if Kiosk mode is on and it launches full screen), and song titles disappear after 60 seconds.
Warnings: Turning off the Pixel Orbiter or the screen saver, turning on a bezel, or locking a music station to one visualizer shows a warning about burn in. There is a do not show this again box you can check for each one if you don't want to be reminded later.
Display care: These help reduce the risk; they cannot remove it. Follow your display maker’s advice, especially for OLED, plasma and CRT screens. All burn in is on by default, with warnings when turned off or using features that increase the risk of burn in. I am not responsible for burn in.
Kiosk Mode
Once everything is set up, you can turn on Kiosk Mode. Scheduling and setup are hidden while the picture, theme and game choices stay available, and the C key no longer opens the Controls Menu and it launches in full screen. In Kiosk Mode the System tab shows only the Kiosk switch and Exit Program. It hides the Channels Tab. Turn Kiosk off to make changes again.
Files, Backups & Problem Reports
Settings: retro_config.json in the app folder holds your settings, channels and schedule positions; back it up before big changes or you can delete it to start fresh but there is a setting in the app that does the same thing.
Media library: main/media_library/library.sqlite3 holds the scan results. Files are recognized by their content, so moved or renamed files keep their results.
Other app data: main/cores (cores and BIOS), main/roms (games, DVD files and saves), main/images (bezels, logos, backgrounds, watermarks, splash art and the key pictures), main/videos, main/audio, main/captions and main/models/whisper.
Problem reports: Close the app, then send the files from the logs\temp folder: app_log.txt, crash_log.txt, crash_log_previous.txt, vlc-debug.log, vlc-debug_previous.log and tester_report.txt, plus retro_config.json for settings problems and logs\update_helper.log for update problems. Say what you did and roughly when. Send this to the Contact/Bug Reports button below.