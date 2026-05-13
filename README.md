# transmission2qbt

A tool which imports / migrates all your torrents from Transmission to
qBittorrent, while trying to preserve as much metadata as possible.

Python is not my native language, so please bear with me.

# Why?

Most of the tools I could find simply used qBt's Web API to import torrents.
This is fine for most usages but I wanted to keep some of the metadata that's
saved in resume files, notably the original "added date" as well as the amount
of data transferred so far and the completion state of the torrent.

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
| 4.0.6        | 5.0.1       | 1.2.19.0   | Linux | https://github.com/undertheironbridge/transmission2qbt/issues/1 |
| 2.94         | 5.0.1       | 2.0.11     | Linux | N/A      |
| 2.94         | 5.1.0       | 2.0.11     | Linux | N/A      |
| 4.1.0        | 5.1.4       | 2.0.12     | Linux | https://github.com/undertheironbridge/transmission2qbt/issues/7 |

# Running

## Prerequisites

First of all, make sure your qBittorrent profile is not set to use the
(currently experimental) SQLite database for storing resume information, as
running this script in this case will not work. The script will quit if it
thinks your qBittorrent instance has this enabled.

This can be checked by going to `Tools > Preferences > Advanced` - the value
for the *Resume data storage type (requires restart)* setting should be
*Fastresume files*. If you change it, restart qBittorrent before running this
script so qBittorrent can export any existing data from SQLite and switches to
Fastresume before starting the migration from Transmission.

## Invocation

Shut down both Transmission and qBittorrent and do :

```
./transmission2qbt.py ~/.config/transmission ~/.local/share/data/qBittorrent/BT_backup
```

## Predicate

The `--predicate` argument accepts a Python expression that can be used for
filtering torrents which are going to be imported to qBt. The parsed torrent
file is named `torrent.value` and its associated Transmission resume data is
`resume.value`. If the expression returns anything other than `True` or throws
an exception, the torrent is skipped.

For example, this will only cause torrents using Debian's tracker whose name
includes `amd64` to be imported :

```
torrent.value[b'announce'] == b'http://bttracker.debian.org:6969/announce'
and b'amd64' in torrent.value[b'info'][b'name']
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

If you use a profile directory specified via the `--profile` commandline option,
then the location you want is `${profile_dir}/qBittorrent/data/BT_backup`.

# Limitations

## Download progress

qBittorrent and Transmission have a different approach to storing information about what data is already on disk:
* Transmission keeps track of the status of each 16KiB block (in `resume[b"progress"][b"blocks]`, which is a bitmask where each bit represents a block). The state of each piece is not explicity stored in the resume file (but is implicitly there since each piece is made of 2^n consecutive blocks).
* qBittorrent keeps track separately of:
  * Which pieces are complete (in `resume[b"pieces"]`).
  * Which blocks are complete in each incomplete piece (in `resume[b"unfinished"]`).
With this in mind, **this script only concerns itself with complete pieces**, i.e. the `pieces` section of the qBittorrent resume file is calculated but the `unfinished` section is omitted.
It should be possible to build it, but:
- This would almost certainly significantly slow down the script. The pieces calculation only leverages data in the resume files, but calculating the unfinished field requires computing checksums of the actual torrent data.
- The only effect of not calculating the field is to **discard partially downloaded pieces**. This should always be a very low amount of data for qBittorrent to download again.
- As a workaround, force-rechecking all incomplete torrents once the migration is complete should recover the information (not tested).

## Incomplete files

If your Transmission is set to append `.part` to incomplete files, make sure
you remove that suffix before running the migration, otherwise qBittorrent won't
detect those files at all. Once qBt detects that a file is incomplete, it will
append `.!qBt` itself if that option is enabled. This command will remove the
`.part` suffix from all files in the current directory recursively :

```
find . -name '*.part' -exec /bin/bash -c 'for i in "$@"; do mv "$i" "${i%.part}"; done;' -- '{}' +
```

* Transmission versions earlier than 4.1.0
(precisely https://github.com/transmission/transmission/commit/1054ba4ab6a40af597e936586ac69a5f27390229)
save the binary form of one of their internal data structures, whose layout is
dependent on the CPU and compiler being used, straight to the resume file. The
current implementation was based on a Transmission compiled with gcc 13.2 on a
x86_64 CPU running Linux.
