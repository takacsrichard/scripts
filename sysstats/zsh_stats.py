#!/usr/bin/env python3
import re
from datetime import datetime
from pathlib import Path
from collections import Counter, OrderedDict

HISTORY = Path.home() / ".zsh_history"
TOP_N   = 10
WINDOW  = 2000   # entries per trend window


# ── Categories ───────────────────────────────────────────────────────────────
# First matching category wins; uncategorized catches the rest.

CATEGORIES = OrderedDict([
    ("nav",      ("Directory navigation",  {
        "cd", "z", "zoxide", "..", "...", "mkcd",
        "pushd", "popd", "home", "docs", "cfd", "dotf",
    })),
    ("list",     ("Listing & disk",        {
        "ls", "l", "eza", "exa", "fd", "find", "tree",
        "gdu", "du", "df", "dfs", "filecount", "pwd", "cpwd",
        "ncdu", "lsblk", "lsof", "stat", "dust",
    })),
    ("file",     ("File / folder ops",     {
        "mv", "rm", "mkdir", "cp", "tar", "extract",
        "rsync", "rclone", "mntdisk", "unmntdisk",
        "chmod", "chown", "touch", "ln", "unzip", "zip",
        "7z", "bzip2", "gzip", "shred", "rename",
        "jdupes", "restic", "archive",
    })),
    ("text",     ("Text & search",         {
        "rg", "grep", "egrep", "fgrep", "head", "tail",
        "cat", "bat", "less", "more", "sed", "awk",
        "diff", "delta", "echo", "sort", "uniq", "wc",
        "cut", "tr", "tee", "xargs", "strings",
        "hexdump", "xxd", "nl", "column", "jq", "yq", "fzf",
        "copy", "identical", "is-subset",
    })),
    ("media",    ("Multimedia",            {
        "mpv", "yt-dlp", "yt", "imv", "ffmpeg", "ffprobe",
        "vlc", "feh", "sxiv", "convert", "identify",
        "mplayer", "m", "oz", "okular", "zathura", "evince", "mpl", "ytq",
    })),
    ("dev",      ("Development & editors", {
        "git", "python", "python3", "nvim", "nano", "vim", "vi",
        "node", "npm", "yarn", "pip", "pip3",
        "cargo", "rustc", "gcc", "g++", "make", "cmake",
        "claude", "go", "julia", "ruby", "perl", "lua",
        "bash", "zsh", "sh", "javac", "java", "n",
        "Rscript", "R", "ginit", "gs", "agy", "cla", "ollama",
    })),
    ("system",   ("System & processes",    {
        "sudo", "systemctl", "journalctl",
        "pkill", "kill", "killall", "ps", "procs",
        "top", "btop", "htop", "bpytop", "watch",
        "hyprctl", "free", "lscpu", "uptime",
        "reboot", "poweroff", "shutdown",
        "dmesg", "uname", "sysctl",
        "fafe", "slep", "bt", "bluetoothctl",
        "checkfs", "kitten", "irqgini",
    })),
    ("network",  ("Network",               {
        "nmcli", "ip", "host", "myip",
        "ping", "curl", "wget",
        "ssh", "scp", "sshfs",
        "nslookup", "dig", "traceroute",
        "netstat", "ss", "nc",
        "vpnon", "vpnoff", "hn", "up",
        "wifitui", "netwatch",
    })),
    ("nix",      ("Nix / packages",        {
        "nix", "nix-shell", "nix-build", "nix-env",
        "nixos-rebuild", "nix-store",
        "home-manager", "just", "cn", "hn",
    })),
    ("security", ("Security & auth",       {
        "rbw", "gpg", "openssl", "ssh-keygen",
        "age", "pass", "bw",
    })),
    ("utils",    ("Utilities & misc",      {
        "qalc", "bc", "cal", "date", "time", "timeout",
        "tldr", "man", "info", "which", "whereis", "type",
        "aliases", "lmk", "track", "sz",
        "c", "o", "t",
        "sourcezsh", "szsh", "zshcfgsrc",
        "cowsay", "todo", "history", "h",
    })),
])


def categorize(cmd):
    for key, (_, cmds) in CATEGORIES.items():
        if cmd in cmds:
            return key
    return "uncategorized"


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_history(path):
    """
    Return list of logical command strings.
    Handles plain format and EXTENDED_HISTORY (: ts:elapsed;cmd).
    Joins backslash-continuation lines into one entry.
    """
    entries = []
    parts   = []
    with open(path, "r", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            # Strip EXTENDED_HISTORY prefix
            m = re.match(r"^: \d+:\d+;(.*)", line)
            if m:
                line = m.group(1)
            if line.endswith("\\"):
                parts.append(line[:-1])
            else:
                parts.append(line)
                entry = " ".join(parts).strip()
                if entry:
                    entries.append(entry)
                parts = []
    if parts:
        entry = " ".join(parts).strip()
        if entry:
            entries.append(entry)
    return entries


def parse_timestamps(path):
    """Return (unix_ts, command) pairs for entries that carry a timestamp."""
    result  = []
    parts   = []
    cur_ts  = None
    with open(path, "r", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = re.match(r"^: (\d+):\d+;(.*)", line)
            if m:
                cur_ts = int(m.group(1))
                line   = m.group(2)
            elif not parts:
                cur_ts = None   # plain entry with no timestamp
            if line.endswith("\\"):
                parts.append(line[:-1])
            else:
                parts.append(line)
                entry = " ".join(parts).strip()
                if entry and cur_ts is not None:
                    result.append((cur_ts, entry))
                parts  = []
                cur_ts = None
    return result


TIME_BLOCKS = [f"{h:02d}:00–{h+4:02d}:00" for h in range(0, 24, 4)]


def time_block(ts):
    h = datetime.fromtimestamp(ts).hour
    s = (h // 4) * 4
    return f"{s:02d}:00–{s+4:02d}:00"


def base_of(entry):
    """First command word, skipping leading FOO=bar env assignments."""
    s = re.sub(r'^([A-Z_][A-Z0-9_]*=[^\s]*\s+)+', '', entry.strip())
    m = re.match(r'(\S+)', s)
    return m.group(1) if m else ""


def count_pipes(cmd):
    """Count | operators that are NOT part of ||."""
    return len(re.findall(r'\|(?!\|)', cmd.replace("||", "\x00")))


def count_ands(cmd):
    return cmd.count("&&")


# ── Helpers ───────────────────────────────────────────────────────────────────

def bar(pct):
    return "█" * int(pct / 4)


def trunc(s, n=46):
    return s[:n] + "…" if len(s) > n else s


def sec_hdr(title):
    pad = max(0, 56 - len(title))
    print(f"\n── {title} {'─' * pad}")


def print_top(title, counter, total, key_fn=str):
    sec_hdr(title)
    print(f"  {'#':>3}  {'command':<46}  {'cnt':>5}  {'%':>6}")
    print(f"  {'─'*3}  {'─'*46}  {'─'*5}  {'─'*6}")
    for rank, (item, cnt) in enumerate(counter.most_common(TOP_N), 1):
        pct = cnt / total * 100
        label = trunc(key_fn(item))
        print(f"  {rank:>3}  {label:<46}  {cnt:>5}  {pct:>5.1f}%")


def print_top_chains(title, counter, total):
    sec_hdr(title)
    print(f"  {'#':>3}  {'A → B':<95}  {'cnt':>5}  {'%':>6}")
    print(f"  {'─'*3}  {'─'*95}  {'─'*5}  {'─'*6}")
    for rank, ((a, b), cnt) in enumerate(counter.most_common(TOP_N), 1):
        pct = cnt / total * 100
        a_s = trunc(a, 44)
        b_s = trunc(b, 44)
        label = f"{a_s}  →  {b_s}"
        print(f"  {rank:>3}  {label:<95}  {cnt:>5}  {pct:>5.1f}%")


# ── Main ──────────────────────────────────────────────────────────────────────

entries = parse_history(HISTORY)
n = len(entries)

if n == 0:
    print(f"No entries found in {HISTORY}")
    raise SystemExit(0)

base_ctr        = Counter()
full_ctr        = Counter()
base_chain_ctr  = Counter()
full_chain_ctr  = Counter()
cat_ctr         = Counter()

has_pipe   = 0   # entries with ≥1 pipe
has_and    = 0   # entries with ≥1 &&
has_neither = 0

pipes_total = 0  # sum of pipe counts in piped entries
ands_total  = 0  # sum of && counts in &&-entries

prev_base = None
prev_full = None

for entry in entries:
    b = base_of(entry)

    if b:
        base_ctr[b] += 1
        cat_ctr[categorize(b)] += 1
    full_ctr[entry] += 1

    if prev_base and b:
        base_chain_ctr[(prev_base, b)] += 1
    if prev_full is not None:
        full_chain_ctr[(prev_full, entry)] += 1

    prev_base = b
    prev_full = entry

    p = count_pipes(entry)
    a = count_ands(entry)

    if p > 0:
        has_pipe  += 1
        pipes_total += p
    if a > 0:
        has_and   += 1
        ands_total  += a
    if p == 0 and a == 0:
        has_neither += 1


# ── Trend windows ────────────────────────────────────────────────────────────

def _window_cats(window):
    ctr = Counter()
    for e in window:
        b = base_of(e)
        if b:
            ctr[categorize(b)] += 1
    return ctr

_recent = entries[-WINDOW:] if n >= WINDOW else entries
_older  = entries[-(2 * WINDOW):-WINDOW] if n >= 2 * WINDOW else []

recent_cat = _window_cats(_recent)
older_cat  = _window_cats(_older)
r_tot = sum(recent_cat.values()) or 1
o_tot = sum(older_cat.values())  or 1


def trend_col(key):
    """Return a fixed-width trend string comparing recent vs older window."""
    if not _older:
        return "    —  "
    r = recent_cat.get(key, 0) / r_tot
    o = older_cat.get(key, 0)  / o_tot
    if o == 0:
        return "  new  " if r > 0 else "    —  "
    rel = (r - o) / o * 100
    if abs(rel) < 0.5:
        return "  ±0%  "
    arrow  = "▲" if rel > 0 else "▼"
    capped = (1 if rel > 0 else -1) * min(abs(rel), 999)
    return f"{arrow}{capped:+.0f}%".ljust(7)


# ── Output ────────────────────────────────────────────────────────────────────

print(f"\n── ZSH History  {HISTORY}  {'─' * max(0, 38 - len(str(HISTORY)))}")
print(f"  Total entries : {n:,}")

print_top("TOP 10 — BASE COMMAND", base_ctr, n)
print_top_chains("TOP 10 — BASE COMMAND CHAINS  (A → B)", base_chain_ctr, n - 1)
print_top("TOP 10 — FULL COMMAND (with args)", full_ctr, n)
print_top_chains("TOP 10 — FULL COMMAND CHAINS  (A → B)", full_chain_ctr, n - 1)

sec_hdr("PIPE & CHAIN BREAKDOWN")
print(f"  {'metric':<40}  {'count':>6}  {'%':>6}")
print(f"  {'─'*40}  {'─'*6}  {'─'*6}")
print(f"  {'entries with ≥1 pipe  |':<40}  {has_pipe:>6}  {has_pipe/n*100:>5.1f}%")
print(f"  {'entries with ≥1 chain &&':<40}  {has_and:>6}  {has_and/n*100:>5.1f}%")
print(f"  {'entries with neither':<40}  {has_neither:>6}  {has_neither/n*100:>5.1f}%")

print()
if has_pipe:
    avg_p = pipes_total / has_pipe
    print(f"  Avg |  per piped entry   : {avg_p:.2f}  (total pipes: {pipes_total})")
if has_and:
    avg_a = ands_total / has_and
    print(f"  Avg && per &&-entry      : {avg_a:.2f}  (total &&:    {ands_total})")
print()

timestamped = parse_timestamps(HISTORY)
ts_total    = len(timestamped)
time_ctr    = Counter(time_block(ts) for ts, _ in timestamped)

_now     = datetime.now().timestamp()
_w2      = _now - 14 * 86400
_w4      = _now - 28 * 86400
_ts_cur  = [(ts, e) for ts, e in timestamped if ts >= _w2]
_ts_prev = [(ts, e) for ts, e in timestamped if _w4 <= ts < _w2]

def _ts_cats(window):
    ctr = Counter()
    for _, e in window:
        b = base_of(e)
        if b:
            ctr[categorize(b)] += 1
    return ctr

_tc_cur      = _ts_cats(_ts_cur)
_tc_prev     = _ts_cats(_ts_prev)
_tc_cur_tot  = sum(_tc_cur.values())
_tc_prev_tot = sum(_tc_prev.values())

cat_total = sum(cat_ctr.values())
trend_note = f"last {WINDOW} vs prev {WINDOW}" if _older else f"need >{2*WINDOW} entries for trend"
sec_hdr(f"COMMAND CATEGORIES  ({trend_note})")
print(f"  {'category':<28}  {'cnt':>6}  {'%':>6}  {'trend':>7}")
print(f"  {'─'*28}  {'─'*6}  {'─'*6}  {'─'*7}")
all_keys = list(CATEGORIES.keys()) + ["uncategorized"]
for key in sorted(all_keys, key=lambda k: cat_ctr.get(k, 0), reverse=True):
    cnt = cat_ctr.get(key, 0)
    if cnt == 0:
        continue
    label = CATEGORIES[key][0] if key != "uncategorized" else "Uncategorized"
    pct = cnt / cat_total * 100
    print(f"  {label:<28}  {cnt:>6}  {pct:>5.1f}%  {trend_col(key):>7}  {bar(pct)}")
print()

no_prev = _tc_prev_tot == 0
prev_hdr = "prev 2w*" if no_prev else " prev 2w"
sec_hdr(f"COMMAND CATEGORIES  (last 2w={_tc_cur_tot} cmds  vs  prev 2w={'N/A' if no_prev else _tc_prev_tot})")
print(f"  {'category':<28}  {'last 2w':>7}  {prev_hdr:>8}  {'delta':>7}")
print(f"  {'─'*28}  {'─'*7}  {'─'*8}  {'─'*7}")
seen = set(_tc_cur) | set(_tc_prev)
for key in sorted(seen, key=lambda k: _tc_cur.get(k, 0), reverse=True):
    c = _tc_cur.get(key, 0)
    p = _tc_prev.get(key, 0)
    if c == 0 and p == 0:
        continue
    lbl = CATEGORIES[key][0] if key in CATEGORIES else "Uncategorized"
    ps  = "     N/A" if no_prev else f"{p:>8}"
    ds  = "     N/A" if no_prev else f"{c - p:>+7}"
    print(f"  {lbl:<28}  {c:>7}  {ps}  {ds}")
print(f"  {'─'*28}  {'─'*7}  {'─'*8}  {'─'*7}")
if no_prev:
    print(f"  {'TOTAL':<28}  {_tc_cur_tot:>7}  {'     N/A'}  {'    N/A'}")
    print(f"  (* prev 2w has no data yet — need {14 - int((_now - min((ts for ts, _ in timestamped), default=_now)) / 86400)} more days of history)")
else:
    print(f"  {'TOTAL':<28}  {_tc_cur_tot:>7}  {_tc_prev_tot:>8}  {_tc_cur_tot - _tc_prev_tot:>+7}")
print()

if ts_total == 0:
    print(f"  (no timestamped entries — enable EXTENDED_HISTORY to get time-of-day stats)")
else:
    sec_hdr(f"BY TIME OF DAY  ({ts_total:,} of {n:,} entries timestamped)")
    print(f"  {'block':<14}  {'cnt':>6}  {'%':>6}")
    print(f"  {'─'*14}  {'─'*6}  {'─'*6}")
    for label in TIME_BLOCKS:
        cnt = time_ctr.get(label, 0)
        pct = cnt / ts_total * 100
        print(f"  {label:<14}  {cnt:>6}  {pct:>5.1f}%  {bar(pct)}")

uncategorized_top = [(cmd, cnt) for cmd, cnt in base_ctr.most_common() if categorize(cmd) == "uncategorized"][:5]
sec_hdr("TOP 5 — UNCATEGORIZED COMMANDS")
print(f"  {'#':>3}  {'command':<46}  {'cnt':>5}  {'%':>6}")
print(f"  {'─'*3}  {'─'*46}  {'─'*5}  {'─'*6}")
for rank, (cmd, cnt) in enumerate(uncategorized_top, 1):
    print(f"  {rank:>3}  {trunc(cmd):<46}  {cnt:>5}  {cnt/n*100:>5.1f}%")
print()
