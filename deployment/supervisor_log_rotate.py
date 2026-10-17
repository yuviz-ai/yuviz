"""Hourly copy-and-truncate rotation of service logs; services append (>>), so they keep writing without a restart."""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# supervisord rotates these itself.
SKIP_SUFFIXES = (".supervisor.log",)
SKIP_NAMES = {"supervisord.log", "crash-watch.log", "log-rotate.log"}


def rotate(path: Path, max_bytes: int, backups: int) -> bool:
    """Idempotent: a file under max_bytes is left alone."""
    if path.stat().st_size <= max_bytes:
        return False
    for i in range(backups - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{i + 1}"))
    shutil.copyfile(path, path.with_name(f"{path.name}.1"))
    with open(path, "r+b") as f:
        f.truncate(0)
    return True


def rotate_dir(log_dir: Path, max_bytes: int, backups: int) -> list[str]:
    rotated = []
    for path in sorted(log_dir.glob("*.log")):
        if path.name in SKIP_NAMES or path.name.endswith(SKIP_SUFFIXES):
            continue
        if rotate(path, max_bytes, backups):
            rotated.append(path.name)
    return rotated


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=Path.home() / ".yuviz" / "logs")
    parser.add_argument("--max-mb", type=float, default=20)
    parser.add_argument("--backups", type=int, default=3)
    args = parser.parse_args()
    max_bytes = int(args.max_mb * 1024 * 1024)
    log(f"rotating {args.dir}/*.log over {args.max_mb:.0f} MB, keeping {args.backups}")
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return
        header = dict(token.split(":", 1) for token in line.split())
        sys.stdin.read(int(header.get("len", 0)))
        try:
            for name in rotate_dir(args.dir, max_bytes, args.backups):
                log(f"rotated {name}")
        except OSError as exc:
            log(f"rotation failed: {exc!r}")
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
