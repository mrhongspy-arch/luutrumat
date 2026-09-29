"""Backups: a zip with a consistent copy of the database, .env and uploaded images.

Restoring is unzipping it into the bot folder (see setup_mac.sh).
"""

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .db import Database

PREFIX = "salesbot-backup-"


def bot_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent


def make_backup(database: Database, db_path: str, dest_dir: Path) -> Path:
    """Write a backup zip into ``dest_dir`` and return its path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = bot_dir(db_path)
    target = dest_dir / f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}.zip"
    partial = target.with_suffix(".zip.part")
    with tempfile.TemporaryDirectory() as tmp:
        # SQLite's backup API gives a consistent snapshot even while the bot is writing.
        snapshot = Path(tmp) / "shop.db"
        with sqlite3.connect(snapshot) as dest:
            database.conn.backup(dest)
        dest.close()
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, Path(db_path).name)
            env = root / ".env"
            if env.exists():
                zf.write(env, ".env")
            uploads = root / "uploads"
            if uploads.is_dir():
                for image in sorted(uploads.iterdir()):
                    if image.is_file():
                        zf.write(image, f"uploads/{image.name}")
    partial.replace(target)  # never leave a half-written zip under the final name
    return target


def prune(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob(f"{PREFIX}*.zip"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
