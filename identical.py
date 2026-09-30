#!/usr/bin/env python3
"""
identical - compare paths for identity, check subsets, verify archives.

Usage:
  identical [MODE] [-rs N | -st C:P] [-star] [-exclude PAT ...] <path1> <path2> [path3 ...]
  identical [MODE] [-ref FILE] <dir>
  identical subset [-q] <dir|tar> <dir|tar>
  identical verifytar [-N n] <archive> <file1> [file2 ...]

Modes (default identical comparison):
  (default)  byte-for-byte via cmp
  -d         diff mode: diff for files, recursive diff for dirs/archives
  -q         quick mode: filename + filesize only, no I/O

Options (default identical comparison):
  -rs N        randomly sample N files from each archive byte-for-byte (2N total)
  -st C:P      auto-sample: C% confident <=P% of files differ  (e.g. -st 95:1)
  -star        N>2: compare all paths against first (hub-and-spoke); default is all-pairs
  -exclude PAT suppress differences whose path contains PAT (repeatable; not with -d)

Single-directory scan (one dir argument):
  Compares all files inside the dir against each other (all-pairs N:M).
  -ref FILE    compare every file against FILE instead (1:N hub-and-spoke)
  -d           use diff -q instead of byte-for-byte cmp
  Prompts for confirmation before running.

Subcommands:
  subset       check A<=B and B<=A subset relationships between two dirs/tar archives
  verifytar    verify disk files exist in an archive with matching content (tar, zip, 7z)

Supported types: plain files, dirs, .tar[.gz|.bz2|.xz|.zst], .tgz, .zip, .7z

Tar indexing: listing a tar's members requires a full sequential header scan
(no index in the format itself). After that scan, identical offers to save
<archive>.identical-index.json next to the archive; if present and still
matching the archive's size+mtime, it's loaded instead of rescanning.
"""

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB I/O chunk size
_tar_scan_cache: dict[str, list[tuple[int, str]]] = {}  # in-process memo, one scan per path per run


# ── type detection ─────────────────────────────────────────────────────────────

def detect_type(path: str) -> str:
    if os.path.isdir(path):
        return 'dir'
    pl = path.lower()
    if re.search(r'\.(tar(\.(gz|bz2|xz|zst))?|tgz)$', pl):
        return 'tar'
    if pl.endswith('.zip'):
        return 'zip'
    if pl.endswith('.7z'):
        return '7z'
    if os.path.isfile(path):
        return 'file'
    return 'unknown'


# ── archive listing helpers ────────────────────────────────────────────────────

def _strip_common_prefix(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Strip a single shared top-level directory prefix from all paths."""
    if not entries:
        return entries
    tops: set[str] = set()
    for _, p in entries:
        slash = p.find('/')
        tops.add(p[:slash] if slash >= 0 else '')
    if len(tops) == 1 and '' not in tops:
        pfx = tops.pop() + '/'
        return [(sz, p[len(pfx):]) for sz, p in entries if p.startswith(pfx)]
    return entries


def _tar_index_path(archive: str) -> str:
    return archive + '.identical-index.json'


def _load_tar_index(archive: str, idx_path: str) -> list[tuple[int, str]] | None:
    if not os.path.isfile(idx_path):
        return None
    try:
        with open(idx_path) as f:
            data = json.load(f)
        st = os.stat(archive)
        if data['size'] != st.st_size or data['mtime'] != int(st.st_mtime):
            print(f'identical: index {idx_path} is stale (archive changed), rescanning',
                  file=sys.stderr)
            return None
        return [(sz, name) for sz, name in data['entries']]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _save_tar_index(idx_path: str, archive: str, entries: list[tuple[int, str]]) -> None:
    st = os.stat(archive)
    data = {
        'archive': os.path.basename(archive),
        'size': st.st_size,
        'mtime': int(st.st_mtime),
        'entries': [[sz, name] for sz, name in entries],
    }
    tmp = idx_path + f'.tmp{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, idx_path)
    except OSError as e:
        print(f'identical: could not write index {idx_path}: {e}', file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _scan_tar(path: str) -> list[tuple[int, str]]:
    """Return [(size, raw_internal_path), ...] for file members, using/offering a sidecar index."""
    if path in _tar_scan_cache:
        return _tar_scan_cache[path]

    idx_path = _tar_index_path(path)
    cached = _load_tar_index(path, idx_path)
    if cached is not None:
        print(f'identical: using cached index {idx_path}', file=sys.stderr)
        _tar_scan_cache[path] = cached
        return cached

    print(f'identical: scanning {path} (no valid index found)...', file=sys.stderr)
    entries: list[tuple[int, str]] = []
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if m.isfile():
                entries.append((m.size, m.name.lstrip('./')))
    _tar_scan_cache[path] = entries

    if sys.stdin.isatty():
        try:
            resp = input(f'identical: save index to {idx_path} for faster future runs? '
                         f'[y/N] ').strip().lower()
        except EOFError:
            resp = 'n'
        if resp == 'y':
            _save_tar_index(idx_path, path, entries)

    return entries


def _list_7z_raw(path: str) -> list[tuple[int, str]]:
    """Parse 7z l -slt output into [(size, ipath), ...]."""
    r = subprocess.run(['7z', 'l', '-slt', path],
                       capture_output=True, text=True, errors='replace')
    entries: list[tuple[int, str]] = []
    cur_path = cur_size = cur_attr = None
    for line in r.stdout.splitlines():
        if line.startswith('Path = '):
            cur_path = line[7:].strip()
            cur_size = cur_attr = None
        elif line.startswith('Size = '):
            val = line[7:].strip()
            cur_size = int(val) if val.isdigit() else 0
        elif line.startswith('Attributes = '):
            cur_attr = line[13:].strip()
            if cur_path and cur_size is not None and cur_attr:
                # skip directory entries (attribute starts with D)
                if not cur_attr.startswith('D'):
                    entries.append((cur_size, cur_path))
            cur_path = cur_size = cur_attr = None
    return entries


def list_entries(path: str, type_: str) -> list[tuple[int, str]]:
    """Return [(size, relpath), ...] for all regular files, prefix-stripped, sorted by path."""
    if type_ == 'dir':
        entries: list[tuple[int, str]] = []
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                fp = os.path.join(root, name)
                try:
                    entries.append((os.path.getsize(fp), os.path.relpath(fp, path)))
                except OSError:
                    pass
        return sorted(entries, key=lambda x: x[1])

    if type_ == 'file':
        return [(os.path.getsize(path), os.path.basename(path))]

    raw: list[tuple[int, str]] = []
    if type_ == 'tar':
        raw = list(_scan_tar(path))
    elif type_ == 'zip':
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.filename.endswith('/'):
                    raw.append((info.file_size, info.filename))
    elif type_ == '7z':
        raw = _list_7z_raw(path)

    return sorted(_strip_common_prefix(raw), key=lambda x: x[1])


def internal_list(path: str, type_: str) -> list[str]:
    """Return raw internal paths with no prefix stripping (./  stripped from tar)."""
    if type_ == 'dir':
        result: list[str] = []
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                result.append(os.path.relpath(os.path.join(root, name), path))
        return result
    if type_ == 'tar':
        return [name for _, name in _scan_tar(path)]
    if type_ == 'zip':
        with zipfile.ZipFile(path) as zf:
            return [i.filename for i in zf.infolist() if not i.filename.endswith('/')]
    if type_ == '7z':
        return [p for _, p in _list_7z_raw(path)]
    return []


def detect_prefix(paths: list[str]) -> str:
    """Return 'top/' if all paths share one top-level directory, else ''."""
    if not paths:
        return ''
    tops: set[str] = set()
    for p in paths:
        idx = p.find('/')
        tops.add(p[:idx] if idx >= 0 else '')
    if len(tops) == 1 and '' not in tops:
        return tops.pop() + '/'
    return ''


# ── streaming / hashing helpers ────────────────────────────────────────────────

def stream_file(archive: str, type_: str, internal: str) -> bytes | None:
    """Read one file from an archive or directory. Returns bytes or None."""
    try:
        if type_ == 'dir':
            with open(os.path.join(archive, internal), 'rb') as f:
                return f.read()
        if type_ == 'tar':
            norm = internal.lstrip('./')
            with tarfile.open(archive) as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.lstrip('./') == norm:
                        fh = tf.extractfile(m)
                        return fh.read() if fh else None
            return None
        if type_ == 'zip':
            with zipfile.ZipFile(archive) as zf:
                return zf.read(internal)
        if type_ == '7z':
            r = subprocess.run(['7z', 'e', archive, '-so', internal],
                               capture_output=True)
            return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _sha256_fh(fh) -> str:
    h = hashlib.sha256()
    while chunk := fh.read(_CHUNK):
        h.update(chunk)
    return h.hexdigest()


def sha256_file(path: str) -> str:
    with open(path, 'rb') as f:
        return _sha256_fh(f)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def files_equal(a: str, b: str) -> bool:
    """Compare two files byte-for-byte without reading both into memory at once."""
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        while True:
            ca, cb = fa.read(_CHUNK), fb.read(_CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True


# ── extraction helper ──────────────────────────────────────────────────────────

def to_workdir(path: str, type_: str, tmpdirs: list[str]) -> str:
    """Extract archive to a temp dir; return it (or the original path for dirs/files)."""
    if type_ in ('dir', 'file'):
        return path
    tmp = tempfile.mkdtemp()
    tmpdirs.append(tmp)
    if type_ == 'tar':
        with tarfile.open(path) as tf:
            tf.extractall(tmp, filter='data')
    elif type_ == 'zip':
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp)
    elif type_ == '7z':
        subprocess.run(['7z', 'x', path, f'-o{tmp}', '-y'], capture_output=True)
    # unwrap single top-level directory (e.g. archive extracted as archive-name/)
    subs = list(Path(tmp).iterdir())
    if len(subs) == 1 and subs[0].is_dir():
        return str(subs[0])
    return tmp


def is_excluded(path: str, excludes: list[str]) -> bool:
    return any(pat in path for pat in excludes)


# ── single-directory scan ──────────────────────────────────────────────────────

def _cmd_scandir(path: str, mode: str, ref: str | None) -> int:
    """Compare all files inside one directory against each other (or against -ref)."""
    all_files: list[str] = []
    for root, _, fnames in os.walk(path):
        for name in sorted(fnames):
            all_files.append(os.path.join(root, name))
    all_files.sort()

    if not all_files:
        print(f'identical: {path}: no files found', file=sys.stderr)
        return 1

    if ref:
        ref = os.path.abspath(ref)
        if not os.path.isfile(ref):
            print(f'identical: -ref: {ref}: not a file', file=sys.stderr)
            return 1
        pairs = [(ref, f) for f in all_files if os.path.abspath(f) != ref]
        desc = f'{len(pairs)} file(s) vs reference {ref}'
    else:
        pairs = [(all_files[a], all_files[b])
                 for a in range(len(all_files)) for b in range(a + 1, len(all_files))]
        desc = f'{len(pairs)} pair(s) (all-vs-all, {len(all_files)} files)'

    tool = 'diff -q' if mode == 'diff' else 'cmp'
    print(f'Scan: {path}')
    print(f'      {desc}')
    print(f'      Method: {tool}')
    if input('Proceed? [y/N] ').strip().lower() != 'y':
        print('Aborted.', file=sys.stderr)
        return 1

    diffs = same = 0
    for a, b in pairs:
        rel_a = os.path.relpath(a, path) if not ref else os.path.basename(a)
        rel_b = os.path.relpath(b, path)
        if mode == 'diff':
            r = subprocess.run(['diff', '-q', a, b], capture_output=True)
            if r.returncode == 0:
                same += 1
            else:
                print(f'Differ: {rel_a}  vs  {rel_b}')
                diffs += 1
        else:
            if files_equal(a, b):
                same += 1
            else:
                print(f'Differ: {rel_a}  vs  {rel_b}')
                diffs += 1

    total = same + diffs
    if diffs == 0:
        print(f'All {total} pair(s) identical')
    else:
        print(f'{diffs}/{total} pair(s) differ')
    return 0 if diffs == 0 else 1


# ── identical subcommand ───────────────────────────────────────────────────────

def cmd_identical(argv: list[str]) -> int:
    mode = 'cmp'
    rs_n = 0
    st_arg: str | None = None
    star = False
    excludes: list[str] = []
    ref_path: str | None = None
    paths: list[str] = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--':
            paths += argv[i + 1:]
            break
        elif a == '-d':
            mode = 'diff'; i += 1
        elif a == '-q':
            mode = 'quick'; i += 1
        elif a == '-rs' and i + 1 < len(argv):
            rs_n = int(argv[i + 1]); i += 2
        elif a == '-st' and i + 1 < len(argv):
            st_arg = argv[i + 1]; i += 2
        elif a == '-star':
            star = True; i += 1
        elif a == '-exclude' and i + 1 < len(argv):
            excludes.append(argv[i + 1]); i += 2
        elif a == '-ref' and i + 1 < len(argv):
            ref_path = argv[i + 1]; i += 2
        elif a.startswith('-'):
            print(f'identical: unknown option {a}', file=sys.stderr)
            return 1
        else:
            paths.append(a); i += 1

    if len(paths) == 0:
        print(__doc__, file=sys.stderr)
        return 1

    if len(paths) == 1:
        if not os.path.isdir(paths[0]):
            print('identical: single-path mode requires a directory', file=sys.stderr)
            return 1
        return _cmd_scandir(paths[0], mode, ref_path)

    if len(paths) < 2:
        print(__doc__, file=sys.stderr)
        return 1

    for p in paths:
        if not os.path.exists(p):
            print(f'identical: {p}: does not exist', file=sys.stderr)
            return 1

    types = [detect_type(p) for p in paths]
    n = len(paths)
    pairs = ([(0, b) for b in range(1, n)] if (star or n == 2)
             else [(a, b) for a in range(n) for b in range(a + 1, n)])
    npairs = len(pairs)
    show_hdrs = npairs > 1
    has_file = any(t == 'file' for t in types)
    has_cont = any(t != 'file' for t in types)

    if mode == 'diff' and excludes:
        print('identical: -d and -exclude are not reliably supported together; '
              'the run will proceed without -exclude.', file=sys.stderr)
        resp = input('Continue without -exclude? [y/N] ').strip().lower()
        if resp != 'y':
            print('Aborted.', file=sys.stderr)
            return 1
        excludes = []

    # -st: compute sample size from confidence + threshold
    if st_arg:
        m = re.match(r'^([0-9]+(?:\.[0-9]+)?):([0-9]+(?:\.[0-9]+)?)$', st_arg)
        if not m:
            print('identical: -st must be C:P (e.g. 95:1)', file=sys.stderr)
            return 1
        conf, thresh = float(m.group(1)), float(m.group(2))
        if not (0 < conf < 100):
            print('identical: -st confidence must be in (0,100)', file=sys.stderr)
            return 1
        if thresh <= 0:
            print('identical: -st threshold must be > 0', file=sys.stderr)
            return 1
        cf = conf / 100.0
        tf_ = min(thresh / 100.0, 0.9999)
        rs_n = math.ceil(math.log(1 - cf) / math.log(1 - tf_))
        ref = internal_list(paths[0], types[0])
        rs_n = min(rs_n, len(ref))
        print(f'Statistical sample: {rs_n} files from each archive '
              f'({conf:.0f}% confidence, <={thresh}% difference threshold)')
        if input('Proceed? [y/N] ').strip().lower() != 'y':
            print('Aborted.', file=sys.stderr)
            return 1

    tmpdirs: list[str] = []
    try:
        # ── random / statistical sample ────────────────────────────────────────
        if rs_n > 0:
            if any(t == 'file' for t in types):
                print('identical: -rs/-st requires dirs or archives, not plain files',
                      file=sys.stderr)
                return 1
            lists = [internal_list(p, t) for p, t in zip(paths, types)]
            pfxs  = [detect_prefix(lst) for lst in lists]
            g_diffs = g_only = g_checked = 0

            for ia, ib in pairs:
                nA, nB = paths[ia], paths[ib]
                pfxA, pfxB = pfxs[ia], pfxs[ib]
                if show_hdrs:
                    print(f'── {nA} vs {nB} ──')
                b_set = {e[len(pfxB):] for e in lists[ib]}
                a_set = {e[len(pfxA):] for e in lists[ia]}
                pd = po_a = po_b = pc = 0

                for entry in random.sample(lists[ia], min(rs_n, len(lists[ia]))):
                    rel = entry[len(pfxA):]
                    pc += 1
                    if rel not in b_set:
                        if not is_excluded(rel, excludes):
                            print(f'Only in {nA} (not {nB}): {rel}')
                            po_a += 1
                    else:
                        da = stream_file(paths[ia], types[ia], entry)
                        db = stream_file(paths[ib], types[ib], pfxB + rel)
                        if da != db and not is_excluded(rel, excludes):
                            print(f'Differ ({nA} vs {nB}): {rel}')
                            pd += 1

                for entry in random.sample(lists[ib], min(rs_n, len(lists[ib]))):
                    rel = entry[len(pfxB):]
                    if rel not in a_set and not is_excluded(rel, excludes):
                        print(f'Only in {nB} (not {nA}): {rel}')
                        po_b += 1

                if pd == 0 and po_a == 0 and po_b == 0:
                    print(f'All {pc} sampled files are identical')
                else:
                    print(f'{pd}/{pc} differ, {po_a} only in {nA}, {po_b} only in {nB}')
                g_diffs += pd; g_only += po_a + po_b; g_checked += pc

            if show_hdrs:
                if g_diffs == 0 and g_only == 0:
                    print(f'All {npairs} pairs identical')
                else:
                    print(f'Summary: {g_diffs}/{g_checked} total differ, '
                          f'{g_only} only in one side across {npairs} pairs')
            return 0

        # ── quick mode ─────────────────────────────────────────────────────────
        if mode == 'quick':
            qlists = [list_entries(p, t) for p, t in zip(paths, types)]
            any_diff = False
            for ia, ib in pairs:
                sa = {r: sz for sz, r in qlists[ia] if not is_excluded(r, excludes)}
                sb = {r: sz for sz, r in qlists[ib] if not is_excluded(r, excludes)}
                diffs: list[str] = []
                for r, sz in sa.items():
                    if r not in sb:
                        diffs.append(f'Only in {paths[ia]}: {r} ({sz} bytes)')
                    elif sb[r] != sz:
                        diffs.append(f'Size differs ({paths[ia]} vs {paths[ib]}): '
                                     f'{r} ({sz} vs {sb[r]} bytes)')
                for r, sz in sb.items():
                    if r not in sa:
                        diffs.append(f'Only in {paths[ib]}: {r} ({sz} bytes)')
                if diffs:
                    any_diff = True
                    if show_hdrs:
                        print(f'── {paths[ia]} vs {paths[ib]} ──')
                    for d in diffs:
                        print(d)
                elif show_hdrs:
                    print(f'{paths[ia]} and {paths[ib]}: identical (metadata)')
            if not any_diff:
                print('Identical (metadata)' if n == 2
                      else f'All {n} paths identical (metadata)')
            return 0

        # ── diff mode ──────────────────────────────────────────────────────────
        if mode == 'diff':
            if has_file and has_cont:
                print('identical: cannot mix plain files with directories/archives',
                      file=sys.stderr)
                return 1
            total_sz = 0
            for p, t in zip(paths, types):
                try:
                    if t == 'dir':
                        total_sz += sum(
                            os.path.getsize(os.path.join(root, f))
                            for root, _, files in os.walk(p) for f in files)
                    else:
                        total_sz += os.path.getsize(p)
                except OSError:
                    pass
            if total_sz > 5 << 30:
                gib = total_sz / (1 << 30)
                print(f'Warning: inputs total {gib:.1f} GiB — -d will extract to temporary disk.',
                      file=sys.stderr)
                if input('Proceed? [y/N] ').strip().lower() != 'y':
                    print('Aborted.', file=sys.stderr)
                    return 1
            workdirs = [to_workdir(p, t, tmpdirs) for p, t in zip(paths, types)]
            ntypes   = ['file' if t == 'file' else 'dir' for t in types]
            for ia, ib in pairs:
                if show_hdrs:
                    print(f'── {paths[ia]} vs {paths[ib]} ──')
                wA, wB, ntA, ntB = workdirs[ia], workdirs[ib], ntypes[ia], ntypes[ib]
                if ntA == 'file' and ntB == 'file':
                    subprocess.run(['diff', wA, wB])
                elif ntA == 'dir' and ntB == 'dir':
                    subprocess.run(['diff', '-qr', wA, wB])
                else:
                    print('identical: -d: cannot diff a plain file against a directory/archive',
                          file=sys.stderr)
                    return 1
            return 0

        # ── cmp mode (default) ─────────────────────────────────────────────────
        if has_file and has_cont:
            print('identical: cannot mix plain files with directories/archives',
                  file=sys.stderr)
            return 1

        if has_file:
            all_same = True
            for ia, ib in pairs:
                if not files_equal(paths[ia], paths[ib]):
                    print(f'Different: {paths[ia]} vs {paths[ib]}')
                    all_same = False
            if all_same:
                print('Identical')
            return 0

        # dirs / archives: extract and compare recursively
        workdirs = [to_workdir(p, t, tmpdirs) for p, t in zip(paths, types)]
        g_diffs = 0
        for ia, ib in pairs:
            wA, wB, nA, nB = workdirs[ia], workdirs[ib], paths[ia], paths[ib]
            diffs = only_a = only_b = total = 0
            for root, _, files in os.walk(wA):
                for name in sorted(files):
                    fp  = os.path.join(root, name)
                    rel = os.path.relpath(fp, wA)
                    total += 1
                    peer = os.path.join(wB, rel)
                    if not os.path.exists(peer):
                        if not is_excluded(rel, excludes):
                            print(f'Only in {nA}: {rel}')
                            only_a += 1
                    elif not files_equal(fp, peer):
                        if not is_excluded(rel, excludes):
                            print(f'Differ ({nA} vs {nB}): {rel}')
                            diffs += 1
            for root, _, files in os.walk(wB):
                for name in sorted(files):
                    fp  = os.path.join(root, name)
                    rel = os.path.relpath(fp, wB)
                    if not os.path.exists(os.path.join(wA, rel)):
                        if not is_excluded(rel, excludes):
                            print(f'Only in {nB}: {rel}')
                            only_b += 1
            g_diffs += diffs + only_a + only_b
            if diffs == 0 and only_a == 0 and only_b == 0:
                print(f'{nA} and {nB}: identical ({total} files)' if show_hdrs
                      else f'Identical ({total} files)')
        if g_diffs == 0 and show_hdrs:
            print(f'All {len(paths)} paths are identical')
        return 0

    finally:
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)


# ── subset subcommand ──────────────────────────────────────────────────────────

def _subset_entries(path: str, type_: str, quick: bool) -> dict[str, str]:
    """Return {relpath: value} where value is file size (quick) or sha256."""
    result: dict[str, str] = {}
    if quick:
        if type_ == 'dir':
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    fp  = os.path.join(root, name)
                    rel = os.path.relpath(fp, path)
                    try:
                        result[rel] = str(os.path.getsize(fp))
                    except OSError:
                        pass
        elif type_ == 'tar':
            for sz, name in _scan_tar(path):
                result[name] = str(sz)
    else:
        if type_ == 'dir':
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    fp  = os.path.join(root, name)
                    rel = os.path.relpath(fp, path)
                    try:
                        result[rel] = sha256_file(fp)
                    except OSError:
                        pass
        elif type_ == 'tar':
            with tarfile.open(path) as tf:
                for m in tf.getmembers():
                    if m.isfile():
                        fh = tf.extractfile(m)
                        if fh:
                            result[m.name.lstrip('./')] = _sha256_fh(fh)
    return result


def cmd_subset(argv: list[str]) -> int:
    quick = False
    if argv and argv[0] == '-q':
        quick = True
        argv = argv[1:]

    if len(argv) != 2:
        print('Usage: identical subset [-q] <dir|tar> <dir|tar>', file=sys.stderr)
        return 1

    srcA, srcB = argv

    def _type(p: str) -> str | None:
        if os.path.isdir(p):
            return 'dir'
        if re.search(r'\.(tar(\.(gz|bz2|xz|zst))?|tgz)$', p.lower()):
            return 'tar'
        print(f'identical subset: {p}: not a directory or tar archive', file=sys.stderr)
        return None

    typeA, typeB = _type(srcA), _type(srcB)
    if typeA is None or typeB is None:
        return 1

    if quick:
        print('(quick mode: path + size only)\n')

    map_a = _subset_entries(srcA, typeA, quick)
    map_b = _subset_entries(srcB, typeB, quick)

    a_total = a_matched = a_missing = a_differ = 0
    for rel, val in map_a.items():
        a_total += 1
        if rel not in map_b:         a_missing += 1
        elif map_b[rel] == val:      a_matched += 1
        else:                        a_differ  += 1

    b_total = b_matched = b_missing = b_differ = 0
    for rel, val in map_b.items():
        b_total += 1
        if rel not in map_a:         b_missing += 1
        elif map_a[rel] == val:      b_matched += 1
        else:                        b_differ  += 1

    a_sub = a_missing == 0 and a_differ == 0
    b_sub = b_missing == 0 and b_differ == 0

    print(f'A: {srcA}  [{typeA}, {a_total} files]')
    print(f'B: {srcB}  [{typeB}, {b_total} files]')
    print()

    def _row(label: str, is_sub: bool,
             matched: int, total: int, missing: int, differ: int) -> str:
        s = f'{label}  {"yes" if is_sub else "no"}'
        if is_sub:
            return s + f'  ({matched}/{total} matched)'
        parts = [f'{matched}/{total} matched']
        if missing: parts.append(f'{missing} missing')
        if differ:  parts.append(f'{differ} differ')
        return s + '  (' + ', '.join(parts) + ')'

    print(_row('A ⊆ B:', a_sub, a_matched, a_total, a_missing, a_differ))
    print(_row('B ⊆ A:', b_sub, b_matched, b_total, b_missing, b_differ))
    print()
    print(f'identical: {"yes" if a_sub and b_sub else "no"}')
    return 0


# ── verifytar subcommand ───────────────────────────────────────────────────────

def _last_n(path: str, n: int) -> str:
    """Return the last n path components joined with '/'."""
    parts = Path(path).parts
    return '/'.join(parts[-n:]) if len(parts) >= n else str(Path(path))


def cmd_verifytar(argv: list[str]) -> int:
    n_comp = 2
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '-N' and i + 1 < len(argv):
            n_comp = int(argv[i + 1]); i += 2
        elif re.match(r'^-\d+$', a):
            # -3 style shorthand
            n_comp = int(a[1:]); i += 1
        else:
            rest.append(a); i += 1
    argv = rest

    if len(argv) < 2:
        print('Usage: identical verifytar [-N n] <archive> <file1> [file2 ...]',
              file=sys.stderr)
        return 1

    archive, files = argv[0], argv[1:]

    if not os.path.isfile(archive):
        print(f'identical verifytar: archive not found: {archive}', file=sys.stderr)
        return 1

    arch_type = detect_type(archive)
    if arch_type not in ('tar', 'zip', '7z'):
        print(f'identical verifytar: unsupported archive type: {archive}',
              file=sys.stderr)
        return 1

    print('Indexing archive (may take a moment for large archives)...\n')

    # build suffix → [raw_internal_path] index using raw (./stripped) paths
    index: dict[str, list[str]] = {}
    for ipath in internal_list(archive, arch_type):
        sfx = _last_n(ipath, n_comp)
        index.setdefault(sfx, []).append(ipath)

    identical = different = not_found = 0

    for filepath in files:
        display = os.path.basename(filepath)
        sfx     = _last_n(filepath, n_comp)
        print(f'{display:<60} ', end='', flush=True)

        matches = index.get(sfx, [])
        if not matches:
            print('NOT FOUND')
            not_found += 1
            continue

        if len(matches) > 1:
            print(f'(multiple matches, using: {matches[0]}) ', end='', flush=True)

        ipath = matches[0]

        if not os.path.isfile(filepath):
            print('ERROR: disk file not readable')
            different += 1
            continue

        disk_hash = sha256_file(filepath)
        data = stream_file(archive, arch_type, ipath)
        if data is None:
            print('ERROR: could not read from archive')
            different += 1
            continue
        arch_hash = sha256_bytes(data)

        if disk_hash == arch_hash:
            print('IDENTICAL')
            identical += 1
        else:
            print(f'DIFFERENT  (disk: {disk_hash[:16]}...  archive: {arch_hash[:16]}...)')
            different += 1

    total = identical + different + not_found
    print(f'\nResults  ({total} files checked)')
    print(f'  Identical : {identical}')
    print(f'  Different : {different}')
    print(f'  Not found : {not_found}')
    return 0


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)

    cmd  = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == 'subset':
        sys.exit(cmd_subset(rest))
    elif cmd == 'verifytar':
        sys.exit(cmd_verifytar(rest))
    else:
        sys.exit(cmd_identical(sys.argv[1:]))


if __name__ == '__main__':
    main()
