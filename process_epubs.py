#!/usr/bin/env python3
"""
Process all epub files in ./epub/ and output audiobooks to ./audiobooks/Title - Author/
"""
import os
import re
import sys
import subprocess
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


def process(epub_path: Path):
    title, author = get_epub_metadata(epub_path)
    folder_name = sanitize(f"{title} - {author}")
    output_dir = AUDIOBOOKS_DIR / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)

    if already_done(output_dir):
        print(f"[skip] {folder_name} (already has .m4b)")
        return True

    print(f"[processing] {epub_path.name}")
    print(f"          -> audiobooks/{folder_name}/")

    cmd = [str(EPUB2TTS_BIN), str(epub_path.resolve()), "--engine", ENGINE] + EXTRA_ARGS
    result = subprocess.run(cmd, cwd=output_dir)

    if result.returncode != 0:
        print(f"[error] Failed processing {epub_path.name} (exit {result.returncode})")
        return False

    print(f"[done] {folder_name}")
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
