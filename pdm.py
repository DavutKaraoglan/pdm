#!/usr/bin/env python3
"""pdm - Package Download Manager for Termux."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

VERSION = "1.0"

CONFIG_PATH = Path(os.environ.get("PDM_CONFIG") or Path.home() / ".config" / "pdm" / "config.json")
QUEUE_PATH = Path(os.environ.get("PDM_QUEUE") or Path.home() / ".local" / "share" / "pdm" / "queue.json")

DEFAULTS = {
    "out": "",
    "conns": 16,
    "limit": "",
    "cookies": "",
    "name": "",
    "jobs": 1,
    "retries": 3,
}

STREAM_EXT = {".m3u8", ".mpd", ".f4m", ".ism"}
PAGE_EXT = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml", ""}
PAGE_TYPES = ("text/html", "application/xhtml", "text/xml", "application/xml")

UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

ARIA_RE = re.compile(
    r"\[#\w+\s+([0-9.]+[KMGi]*B)/([0-9.]+[KMGi]*B)\((\d+)%\).*?DL:([0-9.]+[KMGi]*B)(?:.*?ETA:(\S+?))?\]"
)
YTDLP_RE = re.compile(
    r"\[download\]\s+([0-9.]+)% of\s+~?\s*([0-9.]+[KMGi]*B)(?:\s+at\s+([0-9.]+[KMGi]*B)/s)?(?:\s+ETA\s+(\S+))?"
)
# aria2c's summary readout: [DL:816KiB][#gid 256KiB/8.0MiB(3%)][#gid 0B/0B]
GROUP_SPEED_RE = re.compile(r"^\[DL:(\S+?)\]")
GROUP_RE = re.compile(r"\[#(\w+)\s+[0-9.]+[KMGi]*B/[0-9.]+[KMGi]*B(?:\((\d+)%\))?\]")
NOTE_RE = re.compile(
    r"^(ERROR|WARNING|\[Merger\]|\[ExtractAudio\]|\[FixupM|\[download\] Destination|\[Metadata\])"
)


# --- output ---------------------------------------------------------------

GREEN = "\033[32m"
GRAY = "\033[90m"
RESET = "\033[0m"


class Slots:
    """Keeps each concurrent aria2c download on the same numbered bar.

    aria2c prints one summary line per interval and drops finished downloads
    from it, so a gid vanishing is how a completed file is detected.
    """

    def __init__(self, count: int) -> None:
        self.gid: list[str] = [""] * count
        self.pct = [0] * count
        self.finished = 0
        self.speed = ""

    def update(self, line: str) -> None:
        match = GROUP_SPEED_RE.match(line)
        if match:
            self.speed = match.group(1)
        live = {gid: int(pct or 0) for gid, pct in GROUP_RE.findall(line)}
        for index, gid in enumerate(self.gid):
            if not gid:
                continue
            if gid in live:
                self.pct[index] = live.pop(gid)
            else:
                self.gid[index] = ""
                self.pct[index] = 0
                self.finished += 1
        for gid, pct in live.items():
            if "" not in self.gid:
                break
            index = self.gid.index("")
            self.gid[index] = gid
            self.pct[index] = pct


def short(text: str, room: int) -> str:
    """Trim the middle so the start and the extension both stay readable."""
    if len(text) <= room or room < 8:
        return text[:room] if len(text) > room else text
    keep = room - 3
    head = keep * 2 // 3
    return text[:head] + "..." + text[len(text) - (keep - head):]


def tidy(line: str, room: int) -> str:
    """yt-dlp announces full paths, which wrap into noise on a phone terminal."""
    for pattern, label in (
        (r"^\[download\] Destination: (.+)", "file"),
        (r"^\[Merger\] Merging formats into \"(.+)\"", "merge"),
        (r"^\[ExtractAudio\] Destination: (.+)", "audio"),
        (r"^\[Metadata\] Adding metadata to \"(.+)\"", "tag"),
        (r"^\[ExtractAudio\] Not converting audio .*target format (\S+)", "audio"),
    ):
        match = re.match(pattern, line)
        if match:
            value = match.group(1)
            name = value if "/" not in value else Path(value).name
            return f"{label}  {short(name, max(8, room - len(label) - 2))}"
    return short(line, room)


NOTIFY_ID = "pdm"


def notify(content: str, ongoing: bool = False, title: str = "pdm") -> None:
    if not shutil.which("termux-notification"):
        return
    cmd = ["termux-notification", "--id", NOTIFY_ID, "--title", title, "--content", content]
    if ongoing:
        # --alert-once keeps the phone from buzzing on every refresh.
        cmd += ["--ongoing", "--alert-once"]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def meter(pct: int, cell: int, color: bool) -> tuple[str, str]:
    """A bar plus a colourless twin, since ANSI codes make len() useless."""
    fill = round(cell * max(0, min(100, pct)) / 100)
    done, left = "#" * fill, "-" * (cell - fill)
    body = f"{GREEN}{done}{GRAY}{left}{RESET}" if color else done + left
    return f"[{body}]", f"[{done}{left}]"


class Screen:
    """Progress area of one or two lines that plain notices can print above."""

    def __init__(self, quiet: bool = False, notify: bool = False) -> None:
        self.quiet = quiet
        self.width = shutil.get_terminal_size((60, 20)).columns
        self.dirty = False
        self.color = sys.stdout.isatty()
        self.rows = 0
        self.notify = notify or os.environ.get("PDM_NOTIFY") == "1"
        self.notified = 0.0

    def status(self, pct: float, tail: str, prefix: str = "",
               total: tuple[int, int, float] = ()) -> None:
        if self.quiet:
            return
        rows = [self.total_row(*total)] if total else []
        cell = max(8, self.width - 1 - len(prefix) - len(tail) - 2)
        body, twin = meter(round(pct), cell, self.color)
        rows.append((prefix + body + tail, prefix + twin + tail))
        self.paint(rows)

    def total_row(self, done: int, count: int, pct: float, prefix: str = "") -> tuple[str, str]:
        # Weighted by file count: finished files plus the fraction in flight.
        share = round(min(100, (done * 100 + pct) / max(1, count)))
        tail = f" {share:>3}%  {done}/{count} files"
        head = f"{prefix}total "
        body, twin = meter(share, max(3, self.width - 1 - len(head + tail) - 2), self.color)
        return head + body + tail, head + twin + tail

    def bars(self, slots: Slots, count: int, prefix: str = "") -> None:
        if self.quiet:
            return
        rows = [self.total_row(slots.finished, count, sum(slots.pct), prefix)]

        tail = f" {slots.speed}/s" if slots.speed else ""
        cells = len(slots.pct)
        cell = max(3, (self.width - 1 - len(tail) - cells * 4) // cells)
        text, twin = "", ""
        for index, pct in enumerate(slots.pct, 1):
            body, plain = meter(pct, cell, self.color)
            text += f" {index}{body}"
            twin += f" {index}{plain}"
        rows.append((text.lstrip() + tail, twin.lstrip() + tail))
        self.paint(rows)

    def paint(self, rows: list[tuple[str, str]]) -> None:
        if self.quiet:
            return
        if not self.color:  # a log file gets no cursor moves, so keep one line
            rows = rows[-1:]
        buf = "\033[A" * (self.rows - 1 if self.rows else 0)
        for index, (text, twin) in enumerate(rows):
            buf += "\r" + text + " " * max(0, self.width - 1 - len(twin))
            if index < len(rows) - 1:
                buf += "\n"
        sys.stdout.write(buf)
        sys.stdout.flush()
        self.rows = len(rows)
        self.dirty = True
        if self.notify and time.time() - self.notified > 5:
            self.notified = time.time()
            notify(rows[0][1].strip(), ongoing=True)

    def note(self, text: str) -> None:
        if self.dirty:
            blank = "\r" + " " * (self.width - 1)
            sys.stdout.write(blank + (("\033[A" + blank) * (self.rows - 1)) + "\r")
            self.dirty = False
            self.rows = 0
        print(text, flush=True)

    def done(self) -> None:
        if self.dirty:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.dirty = False
            self.rows = 0


def die(msg: str, code: int = 1) -> None:
    print("pdm: " + msg, file=sys.stderr)
    raise SystemExit(code)


# --- config / queue -------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"pdm: could not read config ({exc}), using defaults", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def load_queue() -> list:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(items: list) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(QUEUE_PATH)


def new_id(items: list) -> str:
    used = {i["id"] for i in items}
    n = 1
    while f"{n:03d}" in used:
        n += 1
    return f"{n:03d}"


# --- environment ----------------------------------------------------------

def js_runtime() -> str:
    """yt-dlp enables only deno by default, so node has to be named explicitly.
    Without a runtime YouTube's nsig challenge cannot be solved and formats are
    missing or throttled."""
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return name
    return ""


def out_dir(cfg: dict, override: str | None = None) -> Path:
    for candidate in (override, cfg.get("out")):
        if candidate:
            path = Path(candidate).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
    for candidate in ("/storage/emulated/0/Download", Path.home() / "storage" / "downloads"):
        path = Path(candidate)
        if path.is_dir() and os.access(path, os.W_OK):
            return path
    path = Path.home() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def probe(url: str) -> tuple[str, int]:
    """Return (content_type, length). Empty type means the probe failed."""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        if method == "GET":
            req.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                length = int(resp.headers.get("Content-Length") or 0)
                return ctype, length
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            continue
    return "", 0


def classify(url: str) -> str:
    """direct = aria2c can fetch it as one file, site = yt-dlp must resolve it."""
    if url.startswith("magnet:") or urlparse(url).scheme in {"ftp", "ftps", "sftp"}:
        return "direct"
    ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if ext in STREAM_EXT:
        return "site"
    if ext == ".torrent":
        return "direct"
    if ext not in PAGE_EXT:
        return "direct"
    ctype, length = probe(url)
    if not ctype:
        return "site"
    if ctype.startswith(PAGE_TYPES):
        return "site"
    return "direct" if length or ctype else "site"


def json_get(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None


def safe_rel(name: str) -> str:
    """Empty if a listed name cannot be trusted as aria2c's out=.

    aria2c follows ../ out of --dir, and the batch file is line based, so a
    line break in a name injects further options.
    """
    if not name or "\n" in name or "\r" in name or name.startswith(("/", "\\", "~")):
        return ""
    parts = [p for p in Path(name).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def keep_safe(files: list[tuple[str, str]], kind: str) -> list[tuple[str, str]]:
    safe = [(url, safe_rel(name)) for url, name in files]
    dropped = [name for (_, name), (_, ok) in zip(files, safe) if not ok]
    if dropped:
        print(f"pdm: skipped {len(dropped)} unsafe {kind} name(s): "
              + ", ".join(repr(d) for d in dropped[:3]), file=sys.stderr)
    return [(url, name) for url, name in safe if name]


def expand(url: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Turn a repo/page URL into (folder, [(file url, relative name), ...])."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]

    if host == "github.com" and len(parts) >= 5 and parts[2] in {"blob", "raw"}:
        owner, repo, _, ref, *rest = parts
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/" + "/".join(rest)
        return "", [(raw, rest[-1])]

    if host == "github.com" and len(parts) == 2:
        owner, repo = parts
        release = json_get(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
        assets = (release or {}).get("assets") or []
        if assets:
            files = keep_safe([(a["browser_download_url"], a["name"]) for a in assets], "asset")
            if files:
                return repo, files
        return "", [(f"https://api.github.com/repos/{owner}/{repo}/zipball/HEAD", f"{repo}.zip")]

    if host == "huggingface.co":
        kind, path = "models", parts
        if parts and parts[0] in {"datasets", "spaces"}:
            kind, path = parts[0], parts[1:]
        prefix = "" if kind == "models" else f"{kind}/"
        # /blob/ serves the HTML file viewer; /resolve/ serves the bytes.
        if len(path) >= 5 and path[2] == "blob":
            owner, repo, _, ref, *rest = path
            direct = (f"https://huggingface.co/{prefix}{owner}/{repo}"
                      f"/resolve/{ref}/" + "/".join(rest))
            return "", [(direct, rest[-1])]
        if len(path) == 2:
            repo = "/".join(path)
            meta = json_get(f"https://huggingface.co/api/{kind}/{repo}")
            names = [s["rfilename"] for s in (meta or {}).get("siblings", []) if s.get("rfilename")]
            base = f"https://huggingface.co/{prefix}{repo}/resolve/main/"
            files = keep_safe([(base + n, n) for n in names], "repo file")
            if files:
                return path[1], files
    return None


def need(tools: list[str]) -> None:
    missing = [t for t in tools if not shutil.which(t)]
    if not missing:
        return
    hints = {"aria2c": "pkg install aria2", "ffmpeg": "pkg install ffmpeg", "yt-dlp": "pip install -U yt-dlp"}
    lines = [f"  {hints.get(t, 'install manually: ' + t)}" for t in missing]
    die("missing tool: " + ", ".join(missing) + "\n" + "\n".join(lines))


# --- process plumbing -----------------------------------------------------

def stream_lines(proc: subprocess.Popen):
    fd = proc.stdout.fileno()
    buf = b""
    while True:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        buf += data
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            text = part.decode("utf-8", "replace").strip()
            if text:
                yield text
    tail = buf.decode("utf-8", "replace").strip()
    if tail:
        yield tail


def run(cmd: list[str], screen: Screen, prefix: str = "", slots: int = 0,
        count: int = 0, done: int = 0) -> tuple[int, list[str]]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log: list[str] = []
    tracker = Slots(slots) if slots > 1 else None
    overall = () if tracker or count < 2 else (done, count)
    try:
        for line in stream_lines(proc):
            log.append(line)
            if tracker:
                if line.startswith("[DL:"):
                    tracker.update(line)
                    screen.bars(tracker, count, prefix)
                    continue
                if ARIA_RE.search(line):  # folded into the bars already
                    continue
            match = ARIA_RE.search(line)
            if match:
                got, total, pct, speed, eta = match.groups()
                screen.status(float(pct), f" {pct:>3}%  {got}/{total}  {speed}/s  {eta or '--'}",
                              prefix, overall and overall + (float(pct),))
                continue
            match = YTDLP_RE.search(line)
            if match:
                pct, total, speed, eta = match.groups()
                screen.status(float(pct), f" {float(pct):>5.1f}%  {total}  {speed or '--'}/s",
                              prefix, overall and overall + (float(pct),))
                continue
            if NOTE_RE.match(line):
                screen.note(prefix + tidy(line, screen.width - len(prefix) - 1))
        proc.wait()
        if tracker and proc.returncode == 0:
            tracker.finished, tracker.pct = count, [100] * slots
            screen.bars(tracker, count, prefix)
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        screen.done()
        raise
    screen.done()
    return proc.returncode, log[-40:]


# --- command building -----------------------------------------------------

# aria2c refuses to start if --max-connection-per-server is above this.
MAX_CONNS = 16

PARALLEL_FILES = 4


def aria2_args(cfg: dict, external: bool = False) -> list[str]:
    conns = str(max(1, min(int(cfg["conns"]), MAX_CONNS)))
    args = [
        "--split", conns,
        "--max-connection-per-server", conns,
        "--min-split-size", "1M",
        "--continue=true",
        # Pre-allocation is pathologically slow on Android's sdcard layer.
        "--file-allocation=none",
        "--auto-file-renaming=false",
        # yt-dlp manages its own .part files and retries fragments itself.
        "--allow-overwrite=true" if external else "--allow-overwrite=false",
        "--max-tries=5",
        "--retry-wait=3",
        "--timeout=30",
        "--connect-timeout=15",
        "--summary-interval=1",
        "--download-result=hide",
        "--console-log-level=warn",
        "--human-readable=true",
    ]
    if cfg.get("limit"):
        args += [f"--max-overall-download-limit={cfg['limit']}"]
    return args


def aria2_cmd(url: str, dest: Path, cfg: dict, opts: dict) -> list[str]:
    cmd = ["aria2c", url, "--dir", str(dest), "--user-agent", UA] + aria2_args(cfg)
    if opts.get("name") or cfg.get("name"):
        cmd += ["--out", opts.get("name") or cfg["name"]]
    if opts.get("cookies") or cfg.get("cookies"):
        cmd += [f"--load-cookies={opts.get('cookies') or cfg['cookies']}"]
    return cmd


def aria2_group_cmd(listing: Path, dest: Path, cfg: dict, opts: dict, jobs: int) -> list[str]:
    # Connections are split across the concurrent downloads instead of added on
    # top, so the link sees the same load but never sits idle between files.
    share = dict(cfg, conns=max(2, int(cfg["conns"]) // jobs))
    cmd = [
        "aria2c",
        "--input-file", str(listing),
        "--dir", str(dest),
        "--user-agent", UA,
        f"--max-concurrent-downloads={jobs}",
    ] + aria2_args(share)
    if opts.get("cookies") or cfg.get("cookies"):
        cmd += [f"--load-cookies={opts.get('cookies') or cfg['cookies']}"]
    return cmd


def ytdlp_cmd(url: str, dest: Path, cfg: dict, opts: dict, external: bool = True) -> list[str]:
    cmd = [
        "yt-dlp", url,
        "--paths", str(dest),
        "--newline",
        "--continue",
        "--no-overwrites",
        "--retries", "5",
        "--fragment-retries", "10",
        # Fragmented streams (DASH/HLS) default to one fragment at a time, which
        # leaves most of the link idle on the native-downloader fallback path.
        "--concurrent-fragments", str(max(1, min(int(cfg["conns"]),
                                                 MAX_CONNS if opts.get("max_speed") else 8))),
        "--embed-metadata",
    ]
    runtime = js_runtime()
    if runtime:
        cmd += ["--js-runtimes", runtime]
    if external:
        inner = " ".join(aria2_args(cfg, external=True))
        cmd += ["--downloader", "aria2c", "--downloader-args", f"aria2c:{inner}"]
    elif cfg.get("limit"):
        cmd += ["--limit-rate", cfg["limit"]]

    template = opts.get("name") or cfg.get("name")
    if opts.get("playlist"):
        cmd += ["--yes-playlist"]
        cmd += ["-o", template or "%(playlist_title)s/%(playlist_index)03d - %(title)s.%(ext)s"]
    else:
        # --no-playlist alone does not stop multi-file items (e.g. archive.org),
        # which yt-dlp reports as a multi_video playlist rather than a playlist URL.
        cmd += ["--no-playlist", "--playlist-items", "1"]
        cmd += ["-o", template or "%(title)s [%(id)s].%(ext)s"]

    if opts.get("audio"):
        cmd += ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]
    elif opts.get("quality"):
        q = int(opts["quality"])
        cmd += ["-f", f"bv*[height<=?{q}]+ba/b[height<=?{q}]/bv*+ba/b"]
    else:
        cmd += ["-f", "bv*+ba/b"]

    cookies = opts.get("cookies") or cfg.get("cookies")
    if cookies:
        cmd += ["--cookies", cookies]
    return cmd


# --- download -------------------------------------------------------------

COOKIE_HOSTS = ("instagram.com", "facebook.com", "x.com", "twitter.com", "reddit.com")


def fetch_direct(url: str, dest: Path, cfg: dict, opts: dict, screen: Screen, prefix: str,
                 count: int = 0, done: int = 0) -> bool:
    need(["aria2c"])
    cmd = aria2_cmd(url, dest, cfg, opts)
    if opts.get("dry_run"):
        screen.note(" ".join(cmd))
        return True
    code, log = run(cmd, screen, prefix, count=count, done=done)
    if code == 13:
        screen.note(f"{prefix}already downloaded, skipped")
        return True
    if code == 0:
        return True
    screen.note(f"{prefix}aria2c failed (code {code})")
    for line in log[-4:]:
        screen.note(f"{prefix}  {line}")
    return False


def fetch_group(files: list, dest: Path, cfg: dict, opts: dict, screen: Screen, prefix: str,
                jobs: int) -> bool:
    need(["aria2c"])
    listing = dest / ".pdm-batch.txt"
    listing.write_text("".join(f"{u}\n  out={n}\n" for u, n in files))
    cmd = aria2_group_cmd(listing, dest, cfg, opts, jobs)
    try:
        if opts.get("dry_run"):
            screen.note(" ".join(cmd))
            return True
        code, log = run(cmd, screen, prefix, slots=jobs, count=len(files))
    finally:
        listing.unlink(missing_ok=True)
    if code in (0, 13):
        return True
    screen.note(f"{prefix}aria2c failed (code {code})")
    for line in log[-4:]:
        screen.note(f"{prefix}  {line}")
    return False


def fetch_site(url: str, dest: Path, cfg: dict, opts: dict, screen: Screen, prefix: str) -> bool:
    need(["yt-dlp"])
    cmd = ytdlp_cmd(url, dest, cfg, opts)
    if opts.get("dry_run"):
        screen.note(" ".join(cmd))
        return True
    code, log = run(cmd, screen, prefix)
    if code == 0:
        return True

    # aria2c cannot follow YouTube's SABR path; yt-dlp's own downloader can.
    blob = "\n".join(log)
    if "aria2c" in blob or "403" in blob:
        screen.note(f"{prefix}external downloader stalled, retrying with the yt-dlp downloader")
        code, log = run(ytdlp_cmd(url, dest, cfg, opts, external=False), screen, prefix)
        if code == 0:
            return True

    screen.note(f"{prefix}failed (code {code})")
    for line in log[-6:]:
        screen.note(f"{prefix}  {line}")
    host = urlparse(url).netloc.lower()
    if any(h in host for h in COOKIE_HOSTS) and not (opts.get("cookies") or cfg.get("cookies")):
        screen.note(f"{prefix}hint: this site may require a login, try --cookies cookies.txt")
    return False


def download(url: str, cfg: dict, opts: dict, screen: Screen, prefix: str = "") -> bool:
    if any(ch.isspace() for ch in url):
        # Usually an unclosed shell quote, so only the first chunk is the real link.
        screen.note(f"{prefix}skipped, link contains a space or line break "
                    f"(quote it): {url.split()[0][:70]}")
        return False

    scheme = urlparse(url).scheme
    if scheme not in {"http", "https", "ftp", "ftps", "sftp"} and not url.startswith("magnet:"):
        screen.note(f"{prefix}skipped, unsupported address: {url}")
        return False

    base = out_dir(cfg, opts.get("out"))
    group = expand(url) if scheme in {"http", "https"} else None
    if group:
        folder, files = group
        dest = base / folder if folder else base
        dest.mkdir(parents=True, exist_ok=True)
        screen.note(f"{prefix}{len(files)} files found -> {dest}")
        jobs = min(PARALLEL_FILES if opts.get("max_speed") else 0, len(files))
        if jobs > 1:
            screen.note(f"{prefix}max speed: {jobs} files in parallel")
            return fetch_group(files, dest, cfg, opts, screen, prefix, jobs)
        ok = True
        for index, (file_url, name) in enumerate(files):
            sub = dict(opts, name=name)
            screen.note(f"{prefix}({index + 1}/{len(files)}) {name}")
            if not fetch_direct(file_url, dest, cfg, sub, screen, prefix,
                                count=len(files), done=index):
                ok = False
        if ok and len(files) > 1 and not opts.get("dry_run"):
            # Tiny trailing files finish without a progress line, so close the bar.
            screen.paint([screen.total_row(len(files), len(files), 0, prefix)])
            screen.done()
        return ok

    kind = "direct" if url.startswith("magnet:") else classify(url)
    if opts.get("max_speed"):
        screen.note(f"{prefix}max speed: {cfg['conns']} connections, no rate limit")
    if kind == "direct" and not (opts.get("audio") or opts.get("quality")):
        screen.note(f"{prefix}direct download -> {base}")
        if fetch_direct(url, base, cfg, opts, screen, prefix):
            return True
        screen.note(f"{prefix}retrying with yt-dlp")

    screen.note(f"{prefix}resolving + downloading -> {base}")
    return fetch_site(url, base, cfg, opts, screen, prefix)


# --- scheduling -----------------------------------------------------------

def parse_when(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"\+(\d+)([smhd])", text)
    if match:
        n = int(match.group(1))
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
        return (datetime.now() + timedelta(**{unit: n})).isoformat(timespec="seconds")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        clock = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        die(f"could not parse time: {text}  (examples: 22:30, +45m, '2026-10-01 09:00')")
    when = datetime.combine(datetime.now().date(), clock)
    if when <= datetime.now():
        when += timedelta(days=1)
    return when.isoformat(timespec="seconds")


def is_due(item: dict) -> bool:
    return not item.get("at") or datetime.fromisoformat(item["at"]) <= datetime.now()


# --- commands -------------------------------------------------------------

def opts_from_args(args: argparse.Namespace) -> dict:
    return {
        "audio": getattr(args, "audio", False),
        "quality": getattr(args, "quality", None),
        "playlist": getattr(args, "playlist", False),
        "out": getattr(args, "out", None),
        "name": getattr(args, "name", None),
        "cookies": getattr(args, "cookies", None),
        "dry_run": getattr(args, "dry_run", False),
        "max_speed": bool(getattr(args, "max_speed", False)),
    }


def apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    if getattr(args, "conns", None):
        if args.conns > MAX_CONNS:
            print(f"pdm: aria2c allows at most {MAX_CONNS} connections, using {MAX_CONNS}",
                  file=sys.stderr)
        cfg["conns"] = args.conns
    if getattr(args, "limit", None):
        cfg["limit"] = args.limit
    if getattr(args, "max_speed", 0):
        cfg["conns"] = MAX_CONNS
        cfg["limit"] = ""
    return cfg


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls = [u.strip() for u in args.urls]
    if getattr(args, "input_file", None):
        path = Path(args.input_file).expanduser()
        if not path.is_file():
            die(f"link file not found: {path}")
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if not urls:
        die("no links given")
    return urls


LOG_DIR = QUEUE_PATH.parent / "logs"


def wake_lock(hold: bool) -> None:
    """Android freezes background processes; the wake lock is what keeps a
    download alive once the screen goes off."""
    tool = "termux-wake-lock" if hold else "termux-wake-unlock"
    if shutil.which(tool):
        subprocess.call([tool], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def detach(args: argparse.Namespace) -> int:
    """Re-run the same command in its own session so closing Termux, or the
    session dying, does not take the download with it."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.log"
    cmd = [sys.executable, str(Path(__file__).resolve())]
    cmd += [a for a in sys.argv[1:] if a not in ("-b", "--background")]
    env = dict(os.environ, PDM_NOTIFY="1")  # the terminal is gone, so notify instead
    with log.open("wb") as handle:
        proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    print(f"running in the background, pid {proc.pid}")
    print(f"log  tail -f {log}")
    print(f"stop kill {proc.pid}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    if getattr(args, "background", False):
        return detach(args)
    cfg = apply_overrides(load_config(), args)
    screen = Screen(quiet=args.quiet, notify=args.notify)
    opts = opts_from_args(args)
    urls = collect_urls(args)
    failed = 0
    total = len(urls)
    for index, url in enumerate(urls, 1):
        prefix = f"[{index}/{total}] " if total > 1 else ""
        if download(url, cfg, opts, screen, prefix):
            if not opts.get("dry_run"):
                screen.note(f"{prefix}done")
        else:
            failed += 1
    if screen.notify and not opts.get("dry_run"):
        notify(f"{total - failed}/{total} done -> {out_dir(cfg, opts.get('out'))}"
               if not failed else f"{failed}/{total} failed")
    return 1 if failed else 0


def cmd_formats(args: argparse.Namespace) -> int:
    need(["yt-dlp"])
    return subprocess.call(["yt-dlp", "-F", "--no-playlist", args.url])


def cmd_add(args: argparse.Namespace) -> int:
    items = load_queue()
    when = parse_when(args.at) if args.at else ""
    opts = opts_from_args(args)
    for url in collect_urls(args):
        item = {
            "id": new_id(items),
            "url": url,
            "opts": opts,
            "at": when,
            "status": "pending",
            "added": datetime.now().isoformat(timespec="seconds"),
            "attempts": 0,
            "error": "",
        }
        items.append(item)
        print(f"queued #{item['id']}" + (f" (at: {when})" if when else ""))
    save_queue(items)
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    items = load_queue()
    if not items:
        print("queue is empty")
        return 0
    for item in items:
        opts = item.get("opts") or {}
        flags = []
        if opts.get("audio"):
            flags.append("audio")
        if opts.get("quality"):
            flags.append(f"{opts['quality']}p")
        if opts.get("playlist"):
            flags.append("playlist")
        tail = f"  [{', '.join(flags)}]" if flags else ""
        when = f"  @{item['at']}" if item.get("at") else ""
        print(f"#{item.get('id', '???')}  {item.get('status', '?'):<11}{when}{tail}")
        print(f"      {item.get('url', '')}")
        if item.get("error"):
            print(f"      error: {item['error']}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    items = load_queue()
    wanted = {i.lstrip("#").lstrip("0") or "0" for i in args.ids}
    keep = [i for i in items if i.get("id", "").lstrip("0") not in wanted]
    removed = len(items) - len(keep)
    save_queue(keep)
    print(f"{removed} entries removed")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    items = load_queue()
    keep = [] if args.all else [i for i in items if i.get("status") != "done"]
    save_queue(keep)
    print(f"{len(items) - len(keep)} entries removed")
    return 0


def run_item(item: dict, cfg: dict, screen: Screen, prefix: str, lock: threading.Lock) -> None:
    opts = dict(item.get("opts") or {})
    ok = False
    try:
        ok = download(item["url"], cfg, opts, screen, prefix)
    except SystemExit:
        # die() is a SystemExit, which "except Exception" would let through and
        # leave the entry marked running for good.
        with lock:
            item["error"] = "aborted, see the message above"
    except Exception as exc:  # a failing item must not kill the rest of the queue
        with lock:
            item["error"] = str(exc)
    with lock:
        item["attempts"] += 1
        if ok:
            item["status"] = "done"
            item["error"] = ""
        elif item["attempts"] >= int(cfg["retries"]):
            item["status"] = "failed"
        else:
            item["status"] = "pending"


def cmd_run(args: argparse.Namespace) -> int:
    if getattr(args, "background", False):
        return detach(args)
    cfg = apply_overrides(load_config(), args)
    jobs = max(1, int(args.jobs or cfg["jobs"]))
    # Parallel items would fight over the single progress line.
    screen = Screen(quiet=args.quiet or jobs > 1, notify=args.notify)
    lock = threading.Lock()

    # An interrupted run leaves entries marked running, and nothing picks those
    # up again.
    stale = load_queue()
    stuck = [i for i in stale if i.get("status") == "running"]
    if stuck:
        for item in stuck:
            item["status"] = "pending"
        save_queue(stale)
        print(f"{len(stuck)} interrupted entries reset to pending")

    while True:
        items = load_queue()
        ready = [i for i in items if i.get("status") == "pending" and is_due(i)]
        if ready:
            batch, ready = ready[:jobs], ready[jobs:]
            while batch:
                for item in batch:
                    item["status"] = "running"
                save_queue(items)
                threads = [
                    threading.Thread(target=run_item, args=(item, cfg, screen, f"#{item['id']} ", lock))
                    for item in batch
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                save_queue(items)
                batch, ready = ready[:jobs], ready[jobs:]

        pending = [i for i in load_queue() if i["status"] == "pending"]
        if not args.daemon:
            if pending and any(is_due(i) for i in pending):
                continue
            waiting = [i for i in pending if not is_due(i)]
            if waiting:
                print(f"{len(waiting)} scheduled entries waiting (use pdm run --daemon to wait for them)")
            return 0
        if not pending:
            print("queue finished")
            if screen.notify:
                notify("queue finished")
            return 0
        try:
            time.sleep(20)
        except KeyboardInterrupt:
            return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not args.pair:
        for key in sorted(DEFAULTS):
            value = cfg.get(key, "")
            print(f"{key} = {value if value != '' else '(default)'}")
        print(f"\nconfig file: {CONFIG_PATH}")
        print(f"download folder: {out_dir(cfg)}")
        return 0
    if len(args.pair) == 1:
        key = args.pair[0]
        if key not in DEFAULTS:
            die(f"unknown setting: {key}")
        print(cfg.get(key, ""))
        return 0
    key, value = args.pair[0], args.pair[1]
    if key not in DEFAULTS:
        die(f"unknown setting: {key}  (valid: {', '.join(sorted(DEFAULTS))})")
    if isinstance(DEFAULTS[key], int):
        try:
            cfg[key] = int(value)
        except ValueError:
            die(f"{key} must be a number")
    else:
        cfg[key] = value
    save_config(cfg)
    print(f"{key} = {cfg[key]}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"pdm {VERSION}")
    print(f"python   {sys.version.split()[0]}")
    missing = []
    for tool, hint in (("aria2c", "pkg install aria2"), ("yt-dlp", "pip install -U yt-dlp"), ("ffmpeg", "pkg install ffmpeg")):
        path = shutil.which(tool)
        if path:
            flag = "-version" if tool == "ffmpeg" else "--version"
            try:
                out = subprocess.run([tool, flag], capture_output=True, text=True, timeout=30)
                text = out.stdout or out.stderr
                version = text.splitlines()[0] if text.strip() else "?"
            except (OSError, subprocess.SubprocessError):
                version = "?"
            print(f"{tool:<9}{version[:50]}")
        else:
            print(f"{tool:<9}MISSING  -> {hint}")
            missing.append(tool)
    runtime = js_runtime()
    if runtime:
        print(f"js       {runtime} ({shutil.which(runtime)})")
    else:
        print("js       MISSING  -> pkg install nodejs-lts  (YouTube needs it)")
    if shutil.which("termux-wake-lock"):
        print("wakelock termux-wake-lock (downloads survive the screen going off)")
    else:
        print("wakelock MISSING  -> pkg install termux-tools  (needed for background)")
    if shutil.which("termux-notification"):
        print("notify   termux-api (needs the Termux:API app too)")
    else:
        print("notify   MISSING  -> pkg install termux-api  (for pdm get -N)")
    target = out_dir(cfg)
    print(f"folder   {target}  ({'writable' if os.access(target, os.W_OK) else 'NOT WRITABLE'})")
    print(f"queue    {len(load_queue())} entries  ({QUEUE_PATH})")
    return 1 if missing else 0


# --- cli ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdm",
        description="pdm - Package Download Manager, built on aria2c + yt-dlp",
    )
    parser.add_argument("-V", "--version", action="version", version=f"pdm {VERSION}")
    subs = parser.add_subparsers(dest="cmd")

    def media_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-a", "--audio", action="store_true", help="audio only (m4a)")
        sub.add_argument("-q", "--quality", type=int, metavar="HEIGHT", help="cap height, e.g. 1080")
        sub.add_argument("-p", "--playlist", action="store_true", help="whole playlist")
        sub.add_argument("-o", "--out", metavar="DIR", help="download folder")
        sub.add_argument("-n", "--name", metavar="NAME", help="file name or yt-dlp template")
        sub.add_argument("-x", "--conns", type=int, metavar="N", help="number of connections")
        sub.add_argument("--limit", metavar="RATE", help="speed limit, e.g. 500K")
        sub.add_argument("-M", "--max-speed", action="store_true",
                         help=f"push the link as hard as it goes "
                              f"(multi-file links {PARALLEL_FILES} at a time)")
        sub.add_argument("--cookies", metavar="FILE", help="cookies file")
        sub.add_argument("--quiet", action="store_true", help="hide the progress line")

    get = subs.add_parser("get", help="download now")
    get.add_argument("urls", nargs="*", metavar="LINK")
    get.add_argument("-i", "--input-file", metavar="FILE", help="one link per line")
    get.add_argument("--dry-run", action="store_true", help="print the command, download nothing")
    get.add_argument("-b", "--background", action="store_true",
                     help="detach and keep going after the terminal closes")
    get.add_argument("-N", "--notify", action="store_true",
                     help="show progress in the Android notification area")
    media_flags(get)
    get.set_defaults(func=cmd_get)

    add = subs.add_parser("add", help="add to the queue")
    add.add_argument("urls", nargs="*", metavar="LINK")
    add.add_argument("-i", "--input-file", metavar="FILE", help="one link per line")
    add.add_argument("--at", metavar="TIME", help="schedule: 22:30, +45m, '2026-10-01 09:00'")
    media_flags(add)
    add.set_defaults(func=cmd_add)

    queue = subs.add_parser("queue", help="list the queue")
    queue.set_defaults(func=cmd_queue)

    run_cmd = subs.add_parser("run", help="process the queue")
    run_cmd.add_argument("-j", "--jobs", type=int, metavar="N", help="downloads at once")
    run_cmd.add_argument("-x", "--conns", type=int, metavar="N", help="number of connections")
    run_cmd.add_argument("--limit", metavar="RATE", help="speed limit")
    run_cmd.add_argument("--daemon", action="store_true", help="wait for scheduled entries")
    run_cmd.add_argument("-b", "--background", action="store_true",
                         help="detach and keep going after the terminal closes")
    run_cmd.add_argument("-N", "--notify", action="store_true",
                         help="show progress in the Android notification area")
    run_cmd.add_argument("--quiet", action="store_true")
    run_cmd.set_defaults(func=cmd_run)

    rm = subs.add_parser("rm", help="remove from the queue")
    rm.add_argument("ids", nargs="+", metavar="ID")
    rm.set_defaults(func=cmd_rm)

    clear = subs.add_parser("clear", help="drop finished entries")
    clear.add_argument("--all", action="store_true", help="drop every entry")
    clear.set_defaults(func=cmd_clear)

    formats = subs.add_parser("formats", help="list available formats")
    formats.add_argument("url", metavar="LINK")
    formats.set_defaults(func=cmd_formats)

    config = subs.add_parser("config", help="show or change settings")
    config.add_argument("pair", nargs="*", metavar="KEY [VALUE]")
    config.set_defaults(func=cmd_config)

    doctor = subs.add_parser("doctor", help="check the environment")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str]) -> int:
    # `pdm <link>` is shorthand for `pdm get <link>`
    if argv and (argv[0].startswith(("http://", "https://", "magnet:"))):
        argv = ["get"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    downloading = args.func in (cmd_get, cmd_run) and not getattr(args, "background", False)
    if downloading:
        wake_lock(True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        if downloading:
            wake_lock(False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
