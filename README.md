<p align="center">
  <a href="https://github.com/Zhekaggg/GameSaveSyns/releases/tag/v1.0.0">
    <b>Download Game Save Sync (.exe)</b>
  </a>
  ·
  <a href="https://github.com/Zhekaggg/GameSaveSyns/releases">
    All Releases
  </a>
  .
    <a href="https://github.com/Zhekaggg/GameSaveSyns/edit/main/README_RU.md">
    RU
  </a>
</p>
# Game Save Sync (GSS)

**Sync PC game saves between computers through the cloud. No USB drives
or manual folder copying.**

Play on both your desktop PC and laptop? Game Save Sync automatically
downloads the latest save before launching the game, starts the game,
and uploads the save to the cloud after you exit. It works with any
cloud-synced folder: OneDrive, Google Drive, Yandex Disk, Dropbox, and
others.

The project was originally made for **Savage Wind** (an unofficial
native PC port of *The Legend of Zelda: Breath of the Wild*), but it
works with any game that stores its saves in a folder.

> The program copies **only the save folder** you specify, not the game
> itself. No game files are included in this repository. This project is
> not affiliated with Nintendo or the creators of Savage Wind.

## Features

-   **Automatic synchronization.** The latest save is downloaded before
    playing, and uploaded to the cloud after the game closes.
-   **Launch the game through a shortcut.** Creates a desktop shortcut
    with the game's icon and a name such as `Game Name (gss)`. Just
    click it to play.
-   **Content-based comparison, not timestamps.** The clocks on two
    computers may differ, so the program compares file contents and
    remembers what was synchronized last time.
-   **Progress-loss protection.** If the save has changed both on the PC
    and in the cloud, the program does not silently overwrite either
    copy. It asks which one to keep, and the other is preserved in a
    backup.
-   **Waits for cloud synchronization.** If the cloud client is still
    downloading files from another computer, the program waits instead
    of using incomplete files.
-   **Backups.** Keeps up to 3 backups. Each folder is named with the
    date and time of the latest save (`2026-11-11__19-03`) and contains
    an `info.txt` file. A backup is not created if no files have
    changed. A new backup replaces the oldest one. You can choose the
    backup folder.
-   **Progress window.** A window with an upload progress indicator
    appears as soon as the game closes.
-   **Game process monitoring.** The program detects when the game has
    actually closed (and has not restarted) and only then uploads the
    save. Also works with games launched through a launcher.
-   **Game icon.** The icon is copied from a game shortcut (`.lnk`,
    `.url`), an `.exe` file, or an image, and is used for the
    application windows and desktop shortcut.
-   **Cloud service files are ignored** (`.tmp.drivedownload`,
    `desktop.ini`, `Thumbs.db`, etc.).
-   **Russian and English interface.** Copy and paste shortcuts work
    with `Ctrl` regardless of the keyboard layout.

## Requirements

-   Windows 10 / 11
-   Python 3.9+ (only required to run from source; `tkinter` is included
    with the standard Python installation)
-   A cloud storage client on both computers (OneDrive, Google Drive,
    Yandex Disk, Dropbox, etc.), signed in to the same account

No third-party Python libraries are required.

## Quick Start

1.  Download `save_claude_1.py` (or the packaged `.exe`) to both
    computers.
2.  Create a separate save folder in your cloud storage, for example
    `OneDrive\GameSaves\SavageWind`. In your cloud client, enable
    **Always keep on this device** for this folder (for OneDrive /
    Google Drive).
3.  Run the program. The settings window (**SETUP**) will open. Specify:
    1.  The game's **save folder**. For Savage Wind, this is the `save`
        folder next to `savagewind.exe`.
    2.  The **cloud folder**.
    3.  The **game executable** (`savagewind.exe`).
    4.  *(Optional)* The **backup folder**.
    5.  *(Optional)* A **game shortcut or image** to use as the icon. If
        left empty, the icon is taken from the game's `.exe` file.
4.  Click **CREATE DESKTOP SHORTCUT**. From now on, launch the game
    using this shortcut.
5.  Repeat the setup on the second computer. The folder paths can be
    different on each computer.

Running the program without arguments always opens the settings window.
Synchronization and game launching are handled by the shortcut.

## How It Works

``` text
Shortcut "Game (gss)"
   │
   ├─ 1. Check the cloud (wait for downloads if necessary)
   │     ├─ cloud copy is newer, local copy unchanged → download
   │     ├─ both copies have changed                  → ask which one to keep
   │     └─ everything matches                       → do nothing
   ├─ 2. Launch the game and monitor its process
   ├─ 3. Game closes (check that it has not restarted)
   ├─ 4. Create a backup (if files have changed)
   └─ 5. Upload to the cloud and show the progress window
```

The following service files are created next to the program:

  -----------------------------------------------------------------------
  File                                Purpose
  ----------------------------------- -----------------------------------
  `config.json`                       Settings

  `sync_state.json`                   Stores what was considered
                                      synchronized last time (do not
                                      delete)

  `sync_log.txt`                      Log of operations with exact
                                      timestamps, useful for
                                      troubleshooting

  `backups/`                          Backup copies (unless another
                                      folder is selected)

  `app_icon.ico`, `app_icon.png`      Icon copied from the game shortcut
  -----------------------------------------------------------------------

A hidden file named `.gss_state.json` is created in the cloud folder. It
records which device uploaded the save and when.

## Command Line

``` text
save_claude_1.py            Open settings (SETUP)
save_claude_1.py run        Synchronize → launch game → upload (used by the shortcut)
save_claude_1.py pull       Download the save from the cloud only
save_claude_1.py push       Upload the save to the cloud only
```

## Additional `config.json` Settings

  -----------------------------------------------------------------------
  Key                                 Description
  ----------------------------------- -----------------------------------
  `process_name`                      Game process name (for example,
                                      `eldenring.exe`); required for
                                      games launched through a launcher

  `game_name`                         Name used for the `Game Name (gss)`
                                      shortcut, if you want a custom name

  `close_grace_seconds`               How many seconds to check after the
                                      game closes to make sure it has not
                                      restarted (default: 5)

  `icon_source`                       Path to a shortcut, `.exe`, or
                                      image used as the icon

  `language`                          Interface language: `ru` or `en`
  -----------------------------------------------------------------------

## Building the `.exe`

``` text
pip install pyinstaller
pyinstaller --noconsole --onedir --name GameSaveSync save_claude_1.py
```

The `--onedir` mode is recommended: it avoids creating a temporary
folder every time the program starts and prevents the
`Failed to remove temporary directory` warning.

## Limitations

-   Windows only. It does not work on phones.
-   Only one game (one set of paths in `config.json`) can be
    synchronized.
-   A cloud client with a local folder is required. The program does not
    connect directly to cloud storage APIs.
-   If the game is played on two computers at the same time using the
    same cloud folder, conflict protection will be triggered, but this
    is best avoided.

## License

Add a license before publishing the repository.
