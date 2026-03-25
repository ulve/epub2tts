#!/usr/bin/env python3
"""
Process all epub files in ./epub/ and output audiobooks to ./audiobooks/Title - Author/
"""
import os
import re
import sys
import subprocess
import time
from pathlib import Path

import ebooklib
from ebooklib import epub

EPUB_DIR = Path(os.environ.get("EPUB_DIR", "epub"))
AUDIOBOOKS_DIR = Path(os.environ.get("AUDIOBOOKS_DIR", "audiobooks"))
ENGINE = os.environ.get("TTS_ENGINE", "kokoro")
EXTRA_ARGS = os.environ.get("EPUB2TTS_ARGS", "--skiplinks --skipfootnotes").split()

# Find epub2tts binary next to the current Python interpreter
EPUB2TTS_BIN = Path(sys.executable).parent / "epub2tts"
if not EPUB2TTS_BIN.exists():
    EPUB2TTS_BIN = "epub2tts"  # fall back to PATH


def get_epub_metadata(epub_path: Path):
    try:
        book = epub.read_epub(str(epub_path))
        title = book.get_metadata("DC", "title")[0][0].strip()
    except Exception:
        title = epub_path.stem
    try:
        author = book.get_metadata("DC", "creator")[0][0].strip()
    except Exception:
        author = "Unknown Author"
    return title, author


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")


def already_done(output_dir: Path) -> bool:
    return any(output_dir.glob("*.m4b"))


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_progress(done: int, total: int, chapter: str, start: float):
    elapsed = time.time() - start
    pct = done / total * 100 if total else 0
    eta_str = ""
    if done > 0 and total > 0:
        eta = elapsed / done * (total - done)
        eta_str = f"  ETA {format_duration(eta)}"
    bar_width = 20
    filled = int(bar_width * done / total) if total else 0
    bar = "#" * filled + "-" * (bar_width - filled)
    chapter_short = chapter[:40].ljust(40)
    line = f"\r[{bar}] {pct:5.1f}%  {done}/{total}  elapsed {format_duration(elapsed)}{eta_str}  {chapter_short}"
    sys.stdout.write(line)
    sys.stdout.flush()


def process(epub_path: Path):
    title, author = get_epub_metadata(epub_path)
    folder_name = sanitize(f"{title} - {author}")
    output_dir = AUDIOBOOKS_DIR / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)

    if already_done(output_dir):
        print(f"[skip] {folder_name} (already has .m4b)")
        return True

    print(f"[processing] {epub_path.name}")
    print(f"          -> audiobooks/{folder_name}/\n")

    cmd = [str(EPUB2TTS_BIN), str(epub_path.resolve()), "--engine", ENGINE] + EXTRA_ARGS
    proc = subprocess.Popen(
        cmd, cwd=output_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )

    total = 0
    done = 0
    current_chapter = ""
    start = time.time()

    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("Number of chapters to read:"):
            try:
                total = int(line.split(":")[1].strip())
            except ValueError:
                pass
        elif line.startswith("initiating chapter:"):
            current_chapter = line.replace("initiating chapter:", "").strip()
            if total:
                print_progress(done, total, current_chapter, start)
        elif line.startswith("done chapter:"):
            done += 1
            if total:
                print_progress(done, total, current_chapter, start)

    proc.wait()
    if total:
        print()  # newline after progress bar

    if proc.returncode != 0:
        print(f"[error] Failed processing {epub_path.name} (exit {proc.returncode})")
        return False

    elapsed = time.time() - start
    print(f"[done] {folder_name}  ({format_duration(elapsed)} total)")
    return True


def main():
    EPUB_DIR.mkdir(exist_ok=True)
    AUDIOBOOKS_DIR.mkdir(exist_ok=True)

    epubs = sorted(EPUB_DIR.glob("*.epub"))
    if not epubs:
        print(f"No .epub files found in {EPUB_DIR.resolve()}")
        sys.exit(0)

    print(f"Found {len(epubs)} epub(s) to process\n")
    failed = []
    for epub_path in epubs:
        ok = process(epub_path)
        if not ok:
            failed.append(epub_path.name)

    if failed:
        print(f"\nFailed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
