"""پشتیبان‌گیری امن و دوره‌ای از بانک اطلاعاتی و فایل‌های بارگذاری‌شده."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from core.privacy import encrypt_clinical_snapshot
from core.privacy import decrypt_clinical_snapshot
from core.time_utils import IRAN_TZ, iran_now


_scheduler_lock = threading.Lock()
_scheduler_started = False
_stop_event = threading.Event()


def backup_directory(app) -> Path:
    folder = Path(app.instance_path) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def database_path(app) -> Path:
    configured = os.environ.get("PAKYAR_BACKUP_DATABASE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(app.instance_path) / "hospital_cleaning.db"


def _temporary_path(folder: Path, suffix: str) -> Path:
    stamp = f"{os.getpid()}_{threading.get_ident()}"
    return folder / f".pakyar_{stamp}{suffix}"


def create_clinical_snapshot(app) -> Path | None:
    """Create a separate Windows-account-encrypted backup of private records."""
    source = Path(app.config.get("CLINICAL_DATABASE_PATH", ""))
    if not source.is_file():
        return None
    folder = Path(app.instance_path) / "clinical_private" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = iran_now().strftime("%Y%m%d_%H%M%S_%f")
    destination = folder / f"clinical_{stamp}.pbk"
    temporary = _temporary_path(folder, ".db")
    try:
        with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(temporary))) as dst:
            src.backup(dst)
            dst.commit()
        protected = encrypt_clinical_snapshot(temporary.read_bytes())
        with open(destination, "xb") as output:
            output.write(protected)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        stale = sorted(folder.glob("clinical_*.pbk"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in stale[30:]:
            old.unlink(missing_ok=True)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def clinical_snapshot_due(app) -> bool:
    latest = latest_clinical_snapshot(app)
    if latest is None:
        return True
    saved_day = datetime.fromtimestamp(latest.stat().st_mtime, tz=IRAN_TZ).date()
    return saved_day < iran_now().date()


def latest_clinical_snapshot(app) -> Path | None:
    folder = Path(app.instance_path) / "clinical_private" / "backups"
    files = sorted(folder.glob("clinical_*.pbk"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _prune_backups(app, keep_count: int = 30) -> None:
    files = sorted(
        backup_directory(app).glob("pakyar_backup_*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_file in files[keep_count:]:
        try:
            old_file.unlink()
        except OSError:
            app.logger.warning("Could not remove old backup: %s", old_file)


def cleanup_stale_temporary_files(app, max_age_seconds: int = 300) -> None:
    now = time.time()
    for temporary in backup_directory(app).glob(".pakyar_*"):
        try:
            if now - temporary.stat().st_mtime > max_age_seconds:
                temporary.unlink()
        except OSError:
            app.logger.warning(
                "Could not remove temporary backup file: %s",
                temporary,
            )


def create_backup(app, reason: str = "manual") -> Path:
    """Create a consistent ZIP backup without stopping the Flask server."""
    source_db = database_path(app)
    if not source_db.exists():
        raise FileNotFoundError(f"Database not found: {source_db}")

    folder = backup_directory(app)
    cleanup_stale_temporary_files(app)
    stamp = iran_now().strftime("%Y%m%d_%H%M%S")
    backup_path = folder / f"pakyar_backup_{stamp}.zip"
    temporary_db = _temporary_path(folder, ".db")
    temporary_zip = _temporary_path(folder, ".zip")

    try:
        # Preserve private records before publishing an ordinary system backup.
        private_snapshot = create_clinical_snapshot(app)
        source_connection = sqlite3.connect(str(source_db))
        destination_connection = sqlite3.connect(str(temporary_db))
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()

        upload_folder = Path(
            app.config.get(
                "UPLOAD_FOLDER",
                Path(app.root_path) / "static" / "uploads",
            )
        )

        with zipfile.ZipFile(
            temporary_zip,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.write(temporary_db, arcname="hospital_cleaning.db")
            if private_snapshot and private_snapshot.is_file():
                archive.write(private_snapshot, arcname="clinical_private.dpapi")
            if upload_folder.exists():
                for file_path in upload_folder.rglob("*"):
                    if file_path.is_file():
                        archive.write(
                            file_path,
                            arcname=str(
                                Path("uploads")
                                / file_path.relative_to(upload_folder)
                            ),
                        )
            archive.writestr(
                "backup-info.txt",
                (
                    "PakYar backup\n"
                    f"created_at={iran_now().isoformat()}\n"
                    f"reason={reason}\n"
                ),
            )

        os.replace(temporary_zip, backup_path)
        _prune_backups(app)
        return backup_path
    finally:
        for temporary in (temporary_db, temporary_zip):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def list_backups(app, limit: int = 30) -> list[dict]:
    records = []
    for path in sorted(
        backup_directory(app).glob("pakyar_backup_*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[:limit]:
        stat = path.stat()
        records.append(
            {
                "name": path.name,
                "path": path,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=IRAN_TZ,
                ),
            }
        )
    return records


def latest_backup(app) -> dict | None:
    records = list_backups(app, limit=1)
    return records[0] if records else None


def validate_backup(app, filename: str) -> dict:
    """Validate a backup archive without changing the live database."""
    folder = backup_directory(app).resolve()
    candidate = (folder / Path(filename).name).resolve()
    result = {
        "name": candidate.name,
        "valid": False,
        "database": False,
        "uploads": 0,
        "clinical": False,
        "errors": [],
    }

    if candidate.parent != folder or not candidate.is_file():
        result["errors"].append("فایل پشتیبان پیدا نشد.")
        return result

    try:
        with zipfile.ZipFile(candidate, "r") as archive:
            members = archive.infolist()
            names = {member.filename for member in members}
            if "clinical_private.dpapi" in names:
                try:
                    protected = archive.read("clinical_private.dpapi")
                    private_data = decrypt_clinical_snapshot(protected)
                    private_db = sqlite3.connect(":memory:")
                    try:
                        private_db.deserialize(private_data)
                        integrity = private_db.execute("PRAGMA integrity_check").fetchone()
                        result["clinical"] = bool(integrity and integrity[0] == "ok")
                    finally:
                        private_db.close()
                    if not result["clinical"]:
                        result["errors"].append("بررسی سلامت پرونده خصوصی در پشتیبان موفق نبود.")
                except Exception as error:
                    result["errors"].append(f"بررسی رمزگشایی پرونده خصوصی ممکن نیست: {error}")
            if "hospital_cleaning.db" not in names:
                result["errors"].append("فایل پایگاه‌داده داخل پشتیبان وجود ندارد.")

            for member in members:
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    result["errors"].append(
                        f"مسیر ناامن داخل فایل پشتیبان: {member.filename}"
                    )
                    break

                if member.filename.startswith("uploads/") and not member.is_dir():
                    result["uploads"] += 1

            if "hospital_cleaning.db" in names:
                temporary_db = _temporary_path(folder, ".validate.db")
                try:
                    with archive.open("hospital_cleaning.db") as source, open(
                        temporary_db,
                        "wb",
                    ) as destination:
                        destination.write(source.read())

                    connection = sqlite3.connect(str(temporary_db))
                    try:
                        integrity = connection.execute(
                            "PRAGMA integrity_check"
                        ).fetchone()
                        result["database"] = bool(
                            integrity and integrity[0] == "ok"
                        )
                        if not result["database"]:
                            result["errors"].append(
                                "بررسی سلامت پایگاه‌داده موفق نبود."
                            )
                    finally:
                        connection.close()
                finally:
                    try:
                        temporary_db.unlink()
                    except FileNotFoundError:
                        pass
    except (OSError, zipfile.BadZipFile) as error:
        result["errors"].append(f"خواندن فایل پشتیبان ممکن نیست: {error}")

    result["valid"] = not result["errors"] and result["database"]
    return result


def backup_is_due(app) -> bool:
    latest = latest_backup(app)
    if not latest:
        return True
    return latest["created_at"].date() < iran_now().date()


def _backup_worker(app, interval_seconds: float) -> None:
    while not _stop_event.is_set():
        try:
            with app.app_context():
                if backup_is_due(app):
                    create_backup(app, reason="automatic")
                elif clinical_snapshot_due(app):
                    create_clinical_snapshot(app)
        except Exception:
            app.logger.exception("Automatic PakYar backup failed")

        _stop_event.wait(interval_seconds)


def start_backup_scheduler(app) -> None:
    """Start one daemon worker per process for daily automatic backups."""
    global _scheduler_started

    enabled = os.environ.get("PAKYAR_BACKUP_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return

    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    try:
        interval_hours = float(
            os.environ.get("PAKYAR_BACKUP_INTERVAL_HOURS", "24")
        )
    except ValueError:
        interval_hours = 24.0

    interval_seconds = max(interval_hours, 1.0) * 3600
    worker = threading.Thread(
        target=_backup_worker,
        args=(app, interval_seconds),
        name="pakyar-backup-scheduler",
        daemon=True,
    )
    worker.start()
