# transmission2qbt

A tool which imports / migrates all your torrents from Transmission to
qBittorrent, while trying to preserve as much metadata as possible.

Python is not my native language, so please bear with me.

# Why?

Most of the tools I could find simply used qBt's Web API to import torrents.
This is fine for most usages but I wanted to keep some of the metadata that's
saved in resume files, notably the original "added date" as well as the amount
of data transferred so far.

# Tested combinations

Resume data is not required or even expected to be kept in the exact same format
between releases. This script operates on resume data directly so it might/will
not work for all possible combinations of
Transmission/qBittorrent/libtorrent-rasterbar.

The table here represents all combinations which have been tested by the author
or have been reported by others as working.

| Transmission | qBittorrent | libtorrent | OS    | Issue    |
| :----------: | :---------: | :--------: | :---: | :------: |
| 4.0.6        | 4.6.2       | 2.0.9      | Linux | N/A      |
| 4.0.6        | 4.6.5       | 1.2.19.0   | Linux | N/A      |
| 4.0.5        | 4.6.5       | 1.2.19.0   | Linux | N/A      |

# Running

Please shut down both Transmission and qBittorrent before running this.

```
./transmission2qbt.py ~/.config/transmission ~/.local/share/data/qBittorrent/BT_backup
```

# Mappings

* Transmission's "labels" become qBittorrent's "tags".
* Transmission's "bandwidth group" becomes qBittorrent's "category".

Paused torrents in Transmission are added as paused and "forced" in qBittorrent,
as otherwise they start at the first run.

# Directories

## Transmission 

```
* If the `TRANSMISSION_HOME` environment variable is set, its value is used.
* On Darwin, `"${HOME}/Library/Application Support/${appname}"` is used.
* On Windows, `"${CSIDL_APPDATA}/${appname}"` is used.
* If `XDG_CONFIG_HOME` is set, `"${XDG_CONFIG_HOME}/${appname}"` is used.
* `"${HOME}/.config/${appname}"` is used as a last resort.
```

as documented in [transmission.h](https://github.com/transmission/transmission/blob/1f10c50979bbbbc8e694b52322dbdbfb25de65cc/libtransmission/transmission.h#L98)
at the time of writing.

## qBittorrent

The location of the "data directory" is not documented anywhere, so it's best to
look at the [source](https://github.com/qbittorrent/qBittorrent/blob/d71086e400162a2a4573a849ac454074e615a7c1/src/base/profile_p.cpp#L87).
Generally, you should look for `BT_backup` in :

* `$HOME/.local/share/data/${qbt_profile_name}` (legacy) or 
  `$HOME/.local/share/${qbt_profile_name}` (non-legacy) on Linux,
* `C:/Users/$USER/AppData/Local/qBittorrent/${qbt_config_name}` on Windows,
* `$HOME/Library/Application Support/qBittorrent/${qbt_config_name}` on macOS.

# Limitations

* I couldn't figure out how to import Transmission data about the already
available pieces, which means that all torrents will automatically be re-checked
when qBittorrent is first run after the migration.

* If your Transmission is set to append `.part` to incomplete files, make sure
you remove that suffix before running the migration, otherwise qBittorrent won't
detect those files at all. Once qBt detects that a file is incomplete, it will
append `.!qBt` itself if that option is enabled. This command will remove the
`.part` suffix from all files in the current directory recursively :

```
find . -name '*.part' -exec /bin/bash -c 'for i in "$@"; do mv "$i" "${i%.part}"; done;' -- '{}' +
```

* This tool assumes that you're running Transmission 4.0+ which, at the time of
writing, stores resume data and torrents as files named after the torrent's
infohash in `resume` and `torrents` subdirectories in its configuration
directory. This has changed over the years so the script most probably won't
work for pre-4.0 versions out of the box.

* This tool assumes that you're _not_ using the - currently experimental - DB
storage for resume data in qBittorrent.

* Transmission saves the binary form of one of its internal data structures,
whose layout is dependent on the CPU and compiler being used, straight to the
resume file. The current implementation was based on a Transmission compiled
with gcc 13.2 on a x86_64 CPU running Linux.
