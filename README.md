# pdm

![platform](https://img.shields.io/badge/platform-Android%20-3ddc84?style=flat&logo=android&logoColor=white)
![python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat&logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/python%20deps-stdlib%20only-brightgreen?style=flat)
![built on](https://img.shields.io/badge/built%20on-aria2c%20%2B%20yt--dlp-orange?style=flat)
![license](https://img.shields.io/badge/license-MIT-blue?style=flat)

Package Download Manager for Termux.

`pdm` is a single stdlib-only Python file that drives `aria2c` and `yt-dlp`. It
decides which of the two a link needs, expands repository pages into their file
lists.


## Why

Most phone downloaders take a file link and fetch it. `pdm` takes the link you
actually have:

| You paste | pdm downloads |
| --- | --- |
| `https://huggingface.co/owner/model` | every file in the repo, in parallel |
| `https://huggingface.co/owner/model/blob/main/x.bin` | the file, not the HTML viewer |
| `https://github.com/owner/repo` | the latest release assets, or the source zip |
| `https://github.com/owner/repo/blob/main/x.py` | the raw file |
| `https://youtu.be/...` | video, audio, or a whole playlist |
| any direct file, magnet or torrent | straight through aria2c |

`curl -L https://huggingface.co/owner/model` gives you 133 KB of HTML. That gap,
not raw speed, is the point of this tool.

## Install

```sh
pkg install -y git
git clone https://github.com/DavutKaraoglan/pdm
cd pdm
sh install.sh
```

`install.sh` installs `aria2`, `ffmpeg`, `yt-dlp`, a JS runtime and
`termux-api`, then symlinks `pdm` into `$PREFIX/bin`, so the command works from
any folder. It skips whatever is already present, so it is safe to re-run.

Give Termux access to your storage once, otherwise downloads stay inside the
Termux home:

```sh
termux-setup-storage
```

Check the result any time:

```sh
pdm doctor
```

```
pdm 1.0
python   3.13.13
aria2c   aria2 version 1.37.0
yt-dlp   2026.08.19
ffmpeg   ffmpeg version 8.1.2
js       node (/data/data/com.termux/files/usr/bin/node)
wakelock termux-wake-lock (downloads survive the screen going off)
notify   termux-api (needs the Termux:API app too)
folder   /storage/emulated/0/Download  (writable)
queue    0 entries
```

### Notifications

`pdm get -N` also needs the **Termux:API** app from
[F-Droid](https://f-droid.org/packages/com.termux.api/); the `termux-api`
package on its own is just the CLI half and the notification will not appear.

### Update

```sh
cd pdm && git pull
pip install -U yt-dlp
```

The symlink keeps pointing at the same file, so nothing else has to be redone.

### Uninstall

```sh
rm "$PREFIX/bin/pdm"
rm -rf ~/.config/pdm ~/.local/share/pdm
```

## Usage

```sh
pdm get <link>                 # download now (the `get` can be omitted)
pdm get -M <link>              # push the link as hard as it goes
pdm get -a <link>              # audio only, m4a
pdm get -q 1080 <link>         # cap the height
pdm get -p <playlist>          # whole playlist
pdm get -o ~/models <link>     # pick the folder
pdm get -i links.txt           # one link per line
pdm formats <link>             # list what the site offers
```

### Running downloads

```sh
pdm status                     # what is downloading, and what stopped halfway
pdm stop 001                   # stop one, or --all
pdm resume                     # continue everything that was interrupted
pdm status --clear             # forget the stopped entries instead
```

Every download is recorded while it runs, so a kill, a crash or a closed
terminal leaves something to go back to:

```
#001  stopped  background  pid 28966  since 19:59:27
      https://huggingface.co/hf-internal-testing/tiny-random-gpt2
      (4/10) model.safetensors

stopped entries can be picked up with: pdm resume
```

`resume` re-runs the same link with the same options; aria2c and yt-dlp both
continue from the part file rather than starting over. Files left behind by a
download pdm has no record of are listed too, since re-sending that link
finishes them the same way.

If you would rather have the space back than finish them:

```sh
pdm clean                      # list the half finished files and their size
pdm clean -y                   # delete them
```

Anything written to in the last 30 seconds is skipped, so a running download
cannot be cleaned out from under itself.

### Queue

```sh
pdm add <link>                 # queue it
pdm add --at 22:30 <link>      # or at a time: 22:30, +45m, '2026-10-01 09:00'
pdm queue                      # list
pdm run                        # work through what is due
pdm run -j 2 --daemon          # two at a time, wait for scheduled entries
pdm rm 003                     # drop one
pdm clear --all                # drop everything
```

### Background

```sh
pdm get -b <link>              # detach; survives closing Termux
pdm get -N <link>              # progress in the notification area
```

`-b` re-runs the command in its own session, writes to
`~/.local/share/pdm/logs/` and turns notifications on, since there is no
terminal left to print to. Either way `pdm` holds a wake lock while downloading,
so the transfer keeps going when the screen goes off.

### Share menu

`install.sh` links `share/termux-url-opener` into `~/bin`, which is what Termux
runs when a link is shared to it. Hit **share -> Termux** in Chrome, YouTube or
anywhere else and a session opens with the link ready:

```
https://youtu.be/jNQXAC9IVRw

1 video   2 audio   3 1080p   4 queue   5 cancel
choice [1]:
```

Enter picks video. The download runs in that session so you can watch the bar,
and `-N` mirrors it to the notification shade; Termux keeps the session alive
while you go back to the browser. `4` only drops the link in the queue for a
later `pdm run`.

### Settings

```sh
pdm config                     # show everything
pdm config out /sdcard/Download
pdm config conns 16            # aria2c allows 1-16
pdm config limit 500K          # speed cap, empty for none
pdm config jobs 2              # queue parallelism
```

Stored in `~/.config/pdm/config.json`; `PDM_CONFIG` and `PDM_QUEUE` override the
paths. Without an `out` setting, downloads land in `/storage/emulated/0/Download`.

## Flags

| Flag | Meaning |
| --- | --- |
| `-M`, `--max-speed` | 16 connections, no rate limit, 4 files at once |
| `-a`, `--audio` | audio only (m4a) |
| `-q`, `--quality N` | cap video height |
| `-p`, `--playlist` | download the whole playlist |
| `-o`, `--out DIR` | download folder |
| `-n`, `--name` | file name or yt-dlp output template |
| `-x`, `--conns N` | connections per server (max 16) |
| `--limit RATE` | speed cap, e.g. `500K` |
| `--cookies FILE` | cookies file for logged-in sites |
| `-b`, `--background` | detach from the terminal |
| `-N`, `--notify` | Android notification progress |
| `--quiet` | no progress line |
| `--dry-run` | print the command, download nothing |

## Notes

- Speed comes from aria2c, not from `pdm`. On a single file it is within a
  second or two of `curl`. On a 10-file repo, `-M` was about 25% faster than a
  `curl` loop in local testing because the link never sits idle between files.
- YouTube needs a JS runtime (`node` or `deno`) for the nsig challenge, or
  formats go missing. `install.sh` handles it.
- When aria2c cannot follow a stream (YouTube's SABR path), `pdm` retries with
  yt-dlp's own downloader automatically.
- Names taken from repository listings are checked before they reach aria2c, so
  a `../` or a newline in a listed file name cannot write outside the folder.
- `pdm.py` is the whole program: standard library only, Python 3.9 or newer.
  Nothing is imported that Termux does not already ship.

## License

MIT, see [LICENSE](LICENSE). `aria2c`, `yt-dlp` and `ffmpeg` are separate
projects under their own licenses.
