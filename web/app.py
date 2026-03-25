#!/usr/bin/env python3
import os
import re
import sys
import time
import signal
import sqlite3
import threading
import subprocess
from pathlib import Path
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, send_file, abort
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR       = Path(os.environ.get("DATA_DIR", "/data"))
UPLOADS_DIR    = DATA_DIR / "uploads"
AUDIOBOOKS_DIR = DATA_DIR / "audiobooks"
DB_PATH        = DATA_DIR / "jobs.db"

ENGINE      = os.environ.get("TTS_ENGINE", "kokoro")
EXTRA_ARGS  = os.environ.get("EPUB2TTS_ARGS", "--skiplinks --skipfootnotes").split()
PASSWORD    = os.environ.get("WEB_PASSWORD", "changeme")
SECRET_KEY  = os.environ.get("FLASK_SECRET_KEY", "changeme")

EPUB2TTS_BIN = Path(sys.executable).parent / "epub2tts"
if not EPUB2TTS_BIN.exists():
    EPUB2TTS_BIN = "epub2tts"

KOKORO_VOICES = [
    ("af_bella",    "American English Female – Bella"),
    ("af_heart",    "American English Female – Heart"),
    ("af_nicole",   "American English Female – Nicole"),
    ("af_sarah",    "American English Female – Sarah"),
    ("af_sky",      "American English Female – Sky"),
    ("am_adam",     "American English Male – Adam"),
    ("am_echo",     "American English Male – Echo"),
    ("am_eric",     "American English Male – Eric"),
    ("am_fenrir",   "American English Male – Fenrir"),
    ("am_liam",     "American English Male – Liam"),
    ("am_michael",  "American English Male – Michael"),
    ("am_onyx",     "American English Male – Onyx"),
    ("bf_alice",    "British English Female – Alice"),
    ("bf_emma",     "British English Female – Emma"),
    ("bf_isabella", "British English Female – Isabella"),
    ("bf_lily",     "British English Female – Lily"),
    ("bm_daniel",   "British English Male – Daniel"),
    ("bm_fable",    "British English Male – Fable"),
    ("bm_george",   "British English Male – George"),
    ("bm_lewis",    "British English Male – Lewis"),
]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
app = Flask(__name__, static_folder=str(_HERE / "static"), template_folder=str(_HERE / "templates"))
app.secret_key = SECRET_KEY

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db


def init_db():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIOBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                title     TEXT NOT NULL,
                author    TEXT NOT NULL,
                epub_name TEXT NOT NULL,
                epub_path TEXT NOT NULL,
                voice     TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'queued',
                progress  INTEGER DEFAULT 0,
                total     INTEGER DEFAULT 0,
                chapter   TEXT DEFAULT '',
                pid       INTEGER DEFAULT NULL,
                output_dir TEXT DEFAULT NULL,
                created_at REAL NOT NULL,
                started_at REAL DEFAULT NULL,
                finished_at REAL DEFAULT NULL,
                error_msg  TEXT DEFAULT NULL,
                log        TEXT DEFAULT NULL
            )
        """)
        # Migrate: add log column if upgrading from older schema
        try:
            db.execute("ALTER TABLE jobs ADD COLUMN log TEXT DEFAULT NULL")
        except Exception:
            pass
        db.commit()
    _import_existing_audiobooks()


def _import_existing_audiobooks():
    """Auto-discover completed audiobooks that exist on disk but not in the DB."""
    with get_db() as db:
        for m4b in AUDIOBOOKS_DIR.rglob("*.m4b"):
            output_dir = str(m4b.parent)
            exists = db.execute(
                "SELECT 1 FROM jobs WHERE output_dir = ? AND status = 'completed'",
                (output_dir,)
            ).fetchone()
            if exists:
                continue
            folder = m4b.parent.name
            if " - " in folder:
                title, author = folder.split(" - ", 1)
            else:
                title, author = folder, "Unknown"
            db.execute("""
                INSERT INTO jobs (title, author, epub_name, epub_path, voice, status,
                                  output_dir, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)
            """, (
                title, author,
                m4b.name, str(m4b),
                "unknown",
                output_dir,
                m4b.stat().st_mtime,
                m4b.stat().st_mtime,
            ))
        db.commit()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")


def get_epub_metadata(epub_path: Path):
    try:
        import ebooklib
        from ebooklib import epub as epublib
        book = epublib.read_epub(str(epub_path))
        title = book.get_metadata("DC", "title")[0][0].strip()
    except Exception:
        title = epub_path.stem
    try:
        author = book.get_metadata("DC", "creator")[0][0].strip()
    except Exception:
        author = "Unknown Author"
    return title, author


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"

# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

_worker_lock = threading.Lock()
_current_job_id = None


def worker_loop():
    global _current_job_id
    while True:
        time.sleep(2)
        with _worker_lock:
            if _current_job_id is not None:
                continue

        with get_db() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                continue
            job_id = row["id"]
            db.execute(
                "UPDATE jobs SET status='processing', started_at=? WHERE id=?",
                (time.time(), job_id)
            )
            db.commit()

        with _worker_lock:
            _current_job_id = job_id

        try:
            _run_job(job_id)
        except Exception as exc:
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status='failed', error_msg=?, finished_at=? WHERE id=?",
                    (str(exc), time.time(), job_id)
                )
                db.commit()
        finally:
            with _worker_lock:
                _current_job_id = None


def _run_job(job_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    epub_path = Path(row["epub_path"])
    voice     = row["voice"]
    title     = row["title"]
    author    = row["author"]

    folder_name = sanitize(f"{title} - {author}")
    output_dir  = AUDIOBOOKS_DIR / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        db.execute(
            "UPDATE jobs SET output_dir=? WHERE id=?",
            (str(output_dir), job_id)
        )
        db.commit()

    cmd = (
        [str(EPUB2TTS_BIN), str(epub_path.resolve()), "--engine", ENGINE,
         "--speaker", voice]
        + EXTRA_ARGS
    )

    proc = subprocess.Popen(
        cmd, cwd=str(output_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )

    with get_db() as db:
        db.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, job_id))
        db.commit()

    total = 0
    done  = 0
    current_chapter = ""
    error_lines = []
    all_lines = []

    for line in proc.stdout:
        line = line.rstrip()
        all_lines.append(line)

        # Check if job was cancelled
        with get_db() as db:
            status = db.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()["status"]
        if status == "cancelled":
            proc.kill()
            proc.wait()
            _cleanup_output(output_dir)
            return

        if line.startswith("Number of chapters to read:"):
            try:
                total = int(line.split(":")[1].strip())
            except ValueError:
                pass
        elif line.startswith("initiating chapter:"):
            current_chapter = line.replace("initiating chapter:", "").strip()
        elif line.startswith("done chapter:"):
            done += 1
        elif any(w in line.lower() for w in ("error", "traceback", "exception", "failed")):
            error_lines.append(line)

        with get_db() as db:
            db.execute(
                "UPDATE jobs SET progress=?, total=?, chapter=? WHERE id=?",
                (done, total, current_chapter, job_id)
            )
            db.commit()

    proc.wait()

    full_log = "\n".join(all_lines)

    with get_db() as db:
        current_status = db.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()["status"]

    if current_status == "cancelled":
        _cleanup_output(output_dir)
        return

    if proc.returncode != 0:
        error_summary = "; ".join(error_lines[-3:]) if error_lines else f"exit {proc.returncode}"
        with get_db() as db:
            db.execute(
                "UPDATE jobs SET status='failed', error_msg=?, log=?, finished_at=? WHERE id=?",
                (error_summary, full_log, time.time(), job_id)
            )
            db.commit()
        return

    with get_db() as db:
        db.execute(
            "UPDATE jobs SET status='completed', log=?, finished_at=?, pid=NULL WHERE id=?",
            (full_log, time.time(), job_id)
        )
        db.commit()


def _cleanup_output(output_dir: Path):
    import shutil
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", voices=KOKORO_VOICES)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    f = request.files.get("epub")
    voice = request.form.get("voice", "af_sky")
    if not f or not f.filename.endswith(".epub"):
        return jsonify({"error": "Please upload a .epub file"}), 400
    if voice not in [v[0] for v in KOKORO_VOICES]:
        return jsonify({"error": "Invalid voice"}), 400

    epub_path = UPLOADS_DIR / f.filename
    f.save(str(epub_path))

    title, author = get_epub_metadata(epub_path)

    with get_db() as db:
        db.execute("""
            INSERT INTO jobs (title, author, epub_name, epub_path, voice, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?)
        """, (title, author, f.filename, str(epub_path), voice, time.time()))
        db.commit()

    return redirect(url_for("index"))


@app.route("/jobs")
@login_required
def jobs():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()

    result = []
    for r in rows:
        output_dir = Path(r["output_dir"]) if r["output_dir"] else None
        m4b_files = list(output_dir.glob("*.m4b")) if output_dir and output_dir.exists() else []
        download_url = None
        if m4b_files:
            rel = m4b_files[0].relative_to(DATA_DIR)
            download_url = url_for("download_file", filepath=str(rel))

        elapsed = None
        if r["started_at"] and r["status"] in ("processing", "completed", "failed"):
            end = r["finished_at"] or time.time()
            elapsed = format_duration(end - r["started_at"])

        result.append({
            "id":           r["id"],
            "title":        r["title"],
            "author":       r["author"],
            "voice":        r["voice"],
            "status":       r["status"],
            "progress":     r["progress"],
            "total":        r["total"],
            "chapter":      r["chapter"],
            "download_url": download_url,
            "elapsed":      elapsed,
            "error_msg":    r["error_msg"],
            "created_at":   r["created_at"],
        })

    return jsonify(result)


@app.route("/cancel/<int:job_id>", methods=["POST"])
@login_required
def cancel(job_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            abort(404)

        if row["status"] == "queued":
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
            db.commit()
            return jsonify({"ok": True})

        if row["status"] == "processing":
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
            db.commit()
            pid = row["pid"]
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if row["output_dir"]:
                _cleanup_output(Path(row["output_dir"]))
            return jsonify({"ok": True})

    return jsonify({"error": "Job cannot be cancelled"}), 400


@app.route("/logs/<int:job_id>")
@login_required
def job_logs(job_id):
    with get_db() as db:
        row = db.execute("SELECT title, author, status, log FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    log = row["log"] or "(no output captured)"
    return (
        f"<html><head><title>Log: {row['title']}</title>"
        "<style>body{{background:#0f1117;color:#ccc;font-family:monospace;padding:1rem}}"
        "pre{{white-space:pre-wrap;word-break:break-all}}</style></head>"
        f"<body><h2>{row['title']} — {row['author']} ({row['status']})</h2>"
        f"<pre>{log}</pre></body></html>"
    )


@app.route("/data/<path:filepath>")
@login_required
def download_file(filepath):
    full_path = DATA_DIR / filepath
    if not full_path.exists() or not full_path.is_file():
        abort(404)
    # Prevent path traversal
    try:
        full_path.relative_to(DATA_DIR)
    except ValueError:
        abort(403)
    return send_file(str(full_path), as_attachment=True)


@app.route("/samples/<voice>.mp3")
@login_required
def voice_sample(voice):
    allowed = {v[0] for v in KOKORO_VOICES}
    if voice not in allowed:
        abort(404)
    sample_path = _HERE / "static" / "samples" / f"{voice}.mp3"
    if not sample_path.exists():
        abort(404)
    return send_file(str(sample_path), mimetype="audio/mpeg")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
