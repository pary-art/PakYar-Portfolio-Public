"""Runtime settings shared by source and packaged PakYar deployments."""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_THREADS = 8


def is_frozen() -> bool:
    """Return whether the app is running from a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_directory() -> Path:
    """Return a persistent directory for the database and server data."""
    configured = os.environ.get("PAKYAR_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    if not is_frozen():
        return project_root() / "instance"

    executable_dir = Path(sys.executable).resolve().parent

    # When the executable is kept in a dist folder, preserve the source
    # database beside the project rather than creating a second copy.
    if executable_dir.name.lower() == "dist":
        project_instance = executable_dir.parent / "instance"
        if project_instance.exists():
            return project_instance

    # For a portable hospital installation, keep data beside the executable.
    return executable_dir / "instance"


def load_secret_key(data_dir: Path) -> str:
    """Load a stable secret without keeping a usable default in source code."""
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return configured

    secret_path = data_dir / ".secret_key"
    try:
        existing = secret_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        with secret_path.open("x", encoding="utf-8") as file:
            file.write(generated)
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return generated
    except FileExistsError:
        try:
            existing = secret_path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
    except OSError:
        pass

    # Only used when the data folder is read-only. A persistent key is used
    # whenever the application can write beside the database.
    return generated


def upload_directory(data_dir: Path, root_path: str) -> Path:
    """Return a persistent upload directory for source and frozen modes."""
    configured = os.environ.get("PAKYAR_UPLOAD_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    if not is_frozen():
        return Path(root_path) / "static" / "uploads"

    # Preserve a legacy source folder when an executable is run from the
    # project's dist folder; otherwise use the portable data directory.
    legacy_folder = data_dir.parent / "static" / "uploads"
    if legacy_folder.exists():
        return legacy_folder

    return data_dir / "uploads"


def database_uri(data_dir: Path) -> str:
    configured = (
        os.environ.get("DATABASE_URL", "").strip()
        or os.environ.get("PAKYAR_DATABASE_URL", "").strip()
    )
    if configured:
        return configured

    database_path = data_dir / "hospital_cleaning.db"
    return f"sqlite:///{database_path.as_posix()}"


def server_host() -> str:
    return os.environ.get("PAKYAR_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def server_port() -> int:
    raw_port = os.environ.get("PAKYAR_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        return DEFAULT_PORT

    return port if 1 <= port <= 65535 else DEFAULT_PORT


def server_threads() -> int:
    """Return the number of Waitress worker threads for network mode."""
    raw_threads = os.environ.get("PAKYAR_THREADS", str(DEFAULT_THREADS)).strip()
    try:
        threads = int(raw_threads)
    except ValueError:
        return DEFAULT_THREADS

    return max(2, min(threads, 64))


def server_mode() -> str:
    """Return the configured deployment mode for diagnostics and launchers."""
    mode = os.environ.get("PAKYAR_MODE", "server").strip().lower()
    return mode if mode in {"server", "client", "standalone"} else "server"


def browser_host() -> str:
    return (
        os.environ.get("PAKYAR_BROWSER_HOST", "127.0.0.1").strip()
        or "127.0.0.1"
    )


def browser_url() -> str:
    return f"http://{browser_host()}:{server_port()}"
