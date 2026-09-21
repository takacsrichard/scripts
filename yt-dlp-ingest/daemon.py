#!/usr/bin/env python3
"""yt-dlp queue daemon — polls SQLite queue and downloads with dynamic worker scaling."""

import os
import re
import sys
import signal
import sqlite3
import subprocess
import threading
import time
import logging
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR  = Path(os.environ.get("YTDLP_DOWNLOAD_DIR", "/staging/ytdlp"))
ARCHIVE_FILE  = DOWNLOAD_DIR / "archive.txt"
LOG_FILE      = DOWNLOAD_DIR / "ytdlp.log"
BASE_DIR      = Path.home() / "global_scripts/yt-dlp-ingest"
DB_FILE       = BASE_DIR / "state" / "queue.db"
PID_FILE      = BASE_DIR / "state" / "daemon.pid"
YTDLP_BIN     = "yt-dlp"
POLL_INTERVAL = 60   # seconds between queue checks
WORKER_IDLE_SLEEP = 5  # seconds a worker sleeps when queue is empty

# Worker scaling: (pending_threshold, total_workers)
# Workers scale UP only; once spawned they live until queue fully drains.
SCALE_TABLE = [
    (10, 4),
    (5,  2),
    (0,  1),
]

# Output template for yt-dlp
OUTPUT_TEMPLATE = str(DOWNLOAD_DIR / "%(uploader)s/%(title)s [%(id)s].%(ext)s")

# ── Globals ────────────────────────────────────────────────────────────────────
_shutdown   = threading.Event()
_kick       = threading.Event()   # set by SIGUSR1 to wake main loop early
_db_lock    = threading.Lock()
_state_lock = threading.Lock()
_workers: list[threading.Thread] = []
_worker_hwm = 0      # high-water-mark for worker count; only increases during a run
_generation = 0      # incremented when queue fully drains; workers from old gen exit

log = logging.getLogger("ytdlp-daemon")


# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    log.addHandler(sh)


# ── Database ───────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT UNIQUE NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                added_at    TEXT DEFAULT (datetime('now')),
                started_at  TEXT,
                finished_at TEXT,
                worker_id   INTEGER,
                error       TEXT,
                retry_count INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_status ON queue(status);
        """)


def _count_statuses() -> dict:
    with _db_lock:
        with _conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def _claim_next(worker_id: int, generation: int) -> tuple[int, str] | None:
    """Atomically claim one pending URL for this worker. Returns (id, url) or None."""
    with _db_lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT id, url FROM queue WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE queue SET status='in_progress', started_at=datetime('now'), worker_id=? WHERE id=?",
                (worker_id, row["id"]),
            )
            c.commit()
            return row["id"], row["url"]
        finally:
            c.close()


def _mark_done(item_id: int):
    with _db_lock:
        with _conn() as c:
            c.execute(
                "UPDATE queue SET status='done', finished_at=datetime('now') WHERE id=?",
                (item_id,),
            )


def _mark_failed(item_id: int, error: str):
    with _db_lock:
        with _conn() as c:
            c.execute(
                "UPDATE queue SET status='failed', finished_at=datetime('now'), error=? WHERE id=?",
                (error[:2000], item_id),
            )


def _mark_already_downloaded(item_id: int):
    """URL was already in the archive — count it as done."""
    with _db_lock:
        with _conn() as c:
            c.execute(
                "UPDATE queue SET status='done', finished_at=datetime('now'), error='already in archive' WHERE id=?",
                (item_id,),
            )


# ── Partial file cleanup ───────────────────────────────────────────────────────
_INTERMEDIATE_RE = re.compile(r"\[.+?\]\.f\d+\.\w+$")

def _clean_part_files():
    """Remove .part files and yt-dlp intermediate format files (e.g. [ID].f251.webm)."""
    for f in DOWNLOAD_DIR.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix == ".part" or _INTERMEDIATE_RE.search(f.name):
            log.warning(f"Removing partial/intermediate file: {f}")
            try:
                f.unlink()
            except OSError as e:
                log.error(f"Could not remove {f}: {e}")


# ── yt-dlp invocation ─────────────────────────────────────────────────────────
def _run_ytdlp(item_id: int, url: str, worker_id: int):
    cmd = [
        YTDLP_BIN,
        "-N", "4",
        "--download-archive", str(ARCHIVE_FILE),
        "-o", OUTPUT_TEMPLATE,
        "--no-colors",
        "--newline",
        "--progress",
        url,
    ]
    log.info(f"[W{worker_id}] START {url}")

    output_lines: list[str] = []
    already_downloaded = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            output_lines.append(line)
            if "has already been recorded in the archive" in line:
                already_downloaded = True
            # Progress percentage lines are noisy — only log at DEBUG
            if re.match(r"\[download\]\s+\d+\.?\d*% of", line):
                log.debug(f"[yt-dlp] {line}")
            else:
                log.info(f"[yt-dlp] {line}")

        proc.wait()
        rc = proc.returncode

        if already_downloaded or rc == 0:
            if already_downloaded:
                log.info(f"[W{worker_id}] ALREADY DOWNLOADED {url}")
                _mark_already_downloaded(item_id)
            else:
                log.info(f"[W{worker_id}] DONE {url}")
                _mark_done(item_id)
        else:
            tail = "\n".join(output_lines[-15:])
            log.error(f"[W{worker_id}] FAILED rc={rc} {url}")
            _clean_part_files()
            _mark_failed(item_id, f"rc={rc}\n{tail}")

    except Exception as exc:
        log.exception(f"[W{worker_id}] EXCEPTION downloading {url}: {exc}")
        _clean_part_files()
        _mark_failed(item_id, str(exc))


# ── Worker threads ─────────────────────────────────────────────────────────────
def _worker(worker_id: int, my_generation: int):
    log.info(f"Worker {worker_id} started (gen={my_generation})")
    while not _shutdown.is_set():
        # Exit if the generation changed (queue drained, new run starting)
        with _state_lock:
            cur_gen = _generation
        if cur_gen != my_generation:
            break

        result = _claim_next(worker_id, my_generation)
        if result is None:
            # Nothing pending; sleep briefly then re-check
            _shutdown.wait(WORKER_IDLE_SLEEP)
            continue

        item_id, url = result
        _run_ytdlp(item_id, url, worker_id)

    log.info(f"Worker {worker_id} exiting (gen={my_generation})")


def _desired_workers(pending: int) -> int:
    for threshold, count in SCALE_TABLE:
        if pending > threshold:
            return count
    return 0


def _spawn_up_to(target: int, generation: int):
    """Spawn additional workers until we reach `target`, reusing the same generation."""
    global _workers
    with _state_lock:
        # Prune dead threads
        _workers = [w for w in _workers if w.is_alive()]
        while len(_workers) < target:
            wid = len(_workers)
            t = threading.Thread(
                target=_worker,
                args=(wid, generation),
                daemon=True,
                name=f"ytdlp-worker-{wid}",
            )
            t.start()
            _workers.append(t)
            log.info(f"Spawned worker {wid} (total alive={len(_workers)}, gen={generation})")


# ── Main loop ─────────────────────────────────────────────────────────────────
def _main_loop():
    global _worker_hwm, _generation, _workers

    while not _shutdown.is_set():
        counts  = _count_statuses()
        pending = counts.get("pending", 0)
        active  = counts.get("in_progress", 0)

        log.debug(
            f"Queue: pending={pending} in_progress={active} "
            f"done={counts.get('done',0)} failed={counts.get('failed',0)}"
        )

        with _state_lock:
            gen = _generation

        if pending == 0 and active == 0:
            # Queue fully drained — retire current workers by bumping generation
            if _worker_hwm > 0:
                log.info("Queue drained — retiring workers")
                with _state_lock:
                    _generation += 1
                    gen = _generation
                    _worker_hwm = 0
                    _workers = []
        else:
            desired = _desired_workers(pending)
            with _state_lock:
                _worker_hwm = max(_worker_hwm, desired)
                target = _worker_hwm

            if target > 0:
                _spawn_up_to(target, gen)

        # Sleep until next poll or a SIGUSR1 kick
        _kick.clear()
        _kick.wait(timeout=POLL_INTERVAL)


# ── Signal handling ───────────────────────────────────────────────────────────
def _handle_term(sig, _frame):
    log.info(f"Signal {sig} received — shutting down")
    _shutdown.set()
    _kick.set()


def _handle_usr1(_sig, _frame):
    log.info("SIGUSR1: waking main loop early")
    _kick.set()


# ── Entry point ───────────────────────────────────────────────────────────────
def _recover_interrupted():
    """On startup, reset any in_progress items back to pending (orphaned from prior run)."""
    with _db_lock:
        with _conn() as c:
            cur = c.execute(
                "UPDATE queue SET status='pending', started_at=NULL, worker_id=NULL "
                "WHERE status='in_progress'"
            )
            if cur.rowcount:
                log.warning(f"Recovered {cur.rowcount} interrupted download(s) → pending")
    _clean_part_files()


def main():
    setup_logging()
    log.info("yt-dlp ingest daemon starting")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    _recover_interrupted()

    PID_FILE.write_text(str(os.getpid()))
    log.info(f"PID {os.getpid()} written to {PID_FILE}")

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT,  _handle_term)
    signal.signal(signal.SIGUSR1, _handle_usr1)

    try:
        _main_loop()
    finally:
        PID_FILE.unlink(missing_ok=True)
        log.info("Daemon stopped")


if __name__ == "__main__":
    main()
