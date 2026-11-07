import logging
import os
import secrets
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, has_request_context, request, session
from sqlalchemy import inspect as sqlalchemy_inspect, text

from core.backups import (
    backup_directory,
    cleanup_stale_temporary_files,
    start_backup_scheduler,
)
from core.models import InventorySnapshot, SupplyInventory, db
from core.privacy import protect_private_storage
from core.migrations import apply_migrations
from core.routes import main as main_blueprint
from core.workload_routes import workload as workload_blueprint
from core.wellbeing_routes import wellbeing as wellbeing_blueprint
from core.reminders import start_reminder_scheduler
from core.runtime import (
    browser_url,
    data_directory,
    database_uri,
    load_secret_key,
    server_host,
    server_port,
    server_mode,
    server_threads,
    upload_directory,
)
from core.time_utils import format_gregorian, format_jalali, utc_now

app = Flask(__name__, instance_relative_config=True)

data_dir = data_directory()
app.instance_path = str(data_dir)
os.makedirs(app.instance_path, exist_ok=True)
clinical_dir = data_dir / "clinical_private"
protect_private_storage(clinical_dir)

upload_dir = upload_directory(data_dir, app.root_path)
os.makedirs(upload_dir, exist_ok=True)

database_url = database_uri(data_dir)
engine_options = {"pool_pre_ping": True}
if database_url.startswith("sqlite:"):
    engine_options["connect_args"] = {
        "check_same_thread": False,
        "timeout": 30,
    }

app.config.update(
    SECRET_KEY=load_secret_key(data_dir),
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_BINDS={'clinical': 'sqlite:///' + (clinical_dir / 'clinical_private.db').as_posix()},
    CLINICAL_DATABASE_PATH=str(clinical_dir / "clinical_private.db"),
    SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    PAKYAR_HOST=server_host(),
    PAKYAR_PORT=server_port(),
    PAKYAR_THREADS=server_threads(),
    PAKYAR_MODE=server_mode(),
    PAKYAR_BROWSER_URL=browser_url(),
    UPLOAD_FOLDER=str(upload_dir),
    BACKUP_FOLDER=str(data_dir / "backups"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_NAME="pakyar_session",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "PAKYAR_COOKIE_SECURE",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"},
    PAKYAR_SESSION_EPOCH=secrets.token_urlsafe(32),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def configure_logging():
    """Write rotating operational logs beside the persistent database."""
    log_directory = data_dir / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = (log_directory / "pakyar.log").resolve()
    marker = str(log_path)

    if not any(
        getattr(handler, "_pakyar_log_path", None) == marker
        for handler in app.logger.handlers
    ):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler._pakyar_log_path = marker
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
            )
        )
        handler.setLevel(logging.INFO)
        app.logger.addHandler(handler)

    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False


configure_logging()


@app.after_request
def apply_security_headers(response):
    """Add baseline browser protections to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Referrer-Policy",
        "strict-origin-when-cross-origin",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "connect-src 'self'",
    )

    # Dashboard and form responses must not be stored by shared browsers.
    if request.path not in {"/health", "/sw.js"}:
        response.headers.setdefault("Cache-Control", "no-store")

    return response

def format_user_date(value, format_str="%Y/%m/%d %H:%M"):
    """Format dates according to the current user's saved preference."""
    mode = session.get("date_mode", "jalali") if has_request_context() else "jalali"
    if mode == "gregorian":
        return format_gregorian(value, format_str)
    if mode == "both":
        return (
            f"{format_jalali(value, format_str)} | "
            f"{format_gregorian(value, format_str)}"
        )
    return format_jalali(value, format_str)


# ثبت فیلتر تاریخ در قالب‌های Jinja؛ نام قدیمی هم برای سازگاری حفظ شده است.
app.jinja_env.filters["jalali"] = format_user_date
app.jinja_env.filters["userdate"] = format_user_date

db.init_app(app)


def error_page(status_code, title, message):
    """Render a standalone error page without querying the current user."""
    template = app.jinja_env.get_template("error.html")
    return template.render(
        status_code=status_code,
        title=title,
        message=message,
        browser_url=app.config["PAKYAR_BROWSER_URL"],
    ), status_code


@app.errorhandler(404)
def not_found(_error):
    return error_page(
        404,
        "صفحه پیدا نشد",
        "این صفحه وجود ندارد یا آدرس آن تغییر کرده است.",
    )


@app.errorhandler(413)
def request_too_large(_error):
    return error_page(
        413,
        "حجم فایل زیاد است",
        "حجم تصویر یا فایل ارسالی نباید بیشتر از ۵ مگابایت باشد.",
    )


@app.errorhandler(500)
def internal_server_error(error):
    db.session.rollback()
    app.logger.error(
        "Unhandled application error: %s",
        error,
        exc_info=True,
    )
    return error_page(
        500,
        "خطای موقت سامانه",
        "سامانه نتوانست این درخواست را کامل کند. اطلاعات خطا در گزارش داخلی ثبت شد.",
    )


def ensure_checklist_schema():
    """Add checklist fields to databases created by older app versions."""
    inspector = sqlalchemy_inspect(db.engine)
    tables = set(inspector.get_table_names())

    if "checklist_items" not in tables or "checklist_completions" not in tables:
        return

    item_columns = {
        column["name"]
        for column in inspector.get_columns("checklist_items")
    }
    completion_columns = {
        column["name"]
        for column in inspector.get_columns("checklist_completions")
    }

    with db.engine.begin() as connection:
        if "frequency" not in item_columns:
            connection.execute(
                text(
                    "ALTER TABLE checklist_items "
                    "ADD COLUMN frequency VARCHAR(20) "
                    "NOT NULL DEFAULT 'daily'"
                )
            )

        if "weekday" not in item_columns:
            connection.execute(
                text(
                    "ALTER TABLE checklist_items "
                    "ADD COLUMN weekday VARCHAR(20)"
                )
            )

        if "period_key" not in completion_columns:
            connection.execute(
                text(
                    "ALTER TABLE checklist_completions "
                    "ADD COLUMN period_key VARCHAR(64) "
                    "NOT NULL DEFAULT ''"
                )
            )

        # نسخه‌های قدیمی چک‌لیست هفتگی فقط مقدار «هفتگی» داشتند.
        # برای اینکه اطلاعات قبلی ناپدید نشود، آن‌ها را به شنبه/صبح منتقل می‌کنیم
        # تا مسئول خدمات یا مسئول بخش بتواند بعداً روز و شیفت دقیق را ویرایش کند.
        connection.execute(
            text(
                "UPDATE checklist_items "
                "SET weekday = 'saturday' "
                "WHERE frequency = 'weekly' "
                "AND (weekday IS NULL OR weekday = '')"
            )
        )
        connection.execute(
            text(
                "UPDATE checklist_items "
                "SET shift = 'صبح' "
                "WHERE frequency = 'weekly' "
                "AND (shift IS NULL OR shift = 'هفتگی')"
            )
        )

        connection.execute(
            text(
                "UPDATE checklist_completions "
                "SET period_key = CAST(checklist_date AS TEXT) "
                "WHERE period_key IS NULL OR period_key = ''"
            )
        )


def ensure_supply_schema():
    """Add newly introduced supply fields to older SQLite databases."""
    inspector = sqlalchemy_inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "supply_usages" not in tables:
        return

    usage_columns = {
        column["name"]
        for column in inspector.get_columns("supply_usages")
    }

    with db.engine.begin() as connection:
        if "estimated_cost" not in usage_columns:
            connection.execute(
                text(
                    "ALTER TABLE supply_usages "
                    "ADD COLUMN estimated_cost FLOAT "
                    "NOT NULL DEFAULT 0"
                )
            )


def ensure_user_preferences_schema():
    """Add appearance settings to databases created by older versions."""
    inspector = sqlalchemy_inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    columns = {
        column["name"]
        for column in inspector.get_columns("users")
    }
    additions = {
        "theme": (
            "ALTER TABLE users ADD COLUMN theme VARCHAR(30) "
            "NOT NULL DEFAULT 'ocean'"
        ),
        "font_scale": (
            "ALTER TABLE users ADD COLUMN font_scale VARCHAR(20) "
            "NOT NULL DEFAULT 'normal'"
        ),
        "date_mode": (
            "ALTER TABLE users ADD COLUMN date_mode VARCHAR(20) "
            "NOT NULL DEFAULT 'jalali'"
        ),
        "compact_mode": (
            "ALTER TABLE users ADD COLUMN compact_mode BOOLEAN "
            "NOT NULL DEFAULT 0"
        ),
        "reduced_motion": (
            "ALTER TABLE users ADD COLUMN reduced_motion BOOLEAN "
            "NOT NULL DEFAULT 0"
        ),
    }

    with db.engine.begin() as connection:
        for name, statement in additions.items():
            if name not in columns:
                connection.execute(text(statement))


def ensure_clinical_schema():
    """Migrate the isolated clinical bind without touching the main database."""
    engine = db.engines['clinical']
    with engine.begin() as connection:
        inspector = sqlalchemy_inspect(connection)
        if 'clinical_notes' not in inspector.get_table_names():
            return
        columns = {column['name'] for column in inspector.get_columns('clinical_notes')}
        if 'section_id' not in columns:
            connection.execute(text('ALTER TABLE clinical_notes ADD COLUMN section_id INTEGER NOT NULL DEFAULT 0'))


def ensure_default_role_display_names():
    """Update the built-in account title without touching custom user names."""
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE users "
                "SET full_name = :new_name "
                "WHERE username = :username "
                "AND full_name = :old_name"
            ),
            {
                "new_name": "مسئول ارشد خدمات سامانه",
                "username": "admin",
                "old_name": "مدیر ارشد سامانه",
            },
        )


def disable_legacy_physician_accounts():
    """Retire the unused physician role without deleting its historical records."""
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE users SET is_active = 0 WHERE role = 'physician' AND is_active <> 0"))


def ensure_inventory_snapshot_baseline():
    """Create one initial trend point for old inventory records."""
    items = SupplyInventory.query.filter_by(is_active=True).all()
    changed = False
    for item in items:
        if not InventorySnapshot.query.filter_by(item_id=item.id).first():
            db.session.add(
                InventorySnapshot(
                    item_id=item.id,
                    quantity=item.current_quantity or 0,
                    source="baseline",
                    note="نقطهٔ شروع پایش موجودی",
                    recorded_by=item.created_by,
                    recorded_at=item.updated_at or utc_now(),
                )
            )
            changed = True

    if changed:
        db.session.commit()


with app.app_context():
    db.create_all()
    apply_migrations(db.engine)
    ensure_checklist_schema()
    ensure_supply_schema()
    ensure_user_preferences_schema()
    ensure_clinical_schema()
    ensure_default_role_display_names()
    disable_legacy_physician_accounts()
    ensure_inventory_snapshot_baseline()

app.register_blueprint(main_blueprint)
app.register_blueprint(workload_blueprint)
app.register_blueprint(wellbeing_blueprint)
backup_directory(app)
cleanup_stale_temporary_files(app)
from core.backups import clinical_snapshot_due, create_clinical_snapshot
with app.app_context():
    if clinical_snapshot_due(app):
        try:
            create_clinical_snapshot(app)
        except Exception:
            app.logger.exception('Initial private clinical backup failed')
start_backup_scheduler(app)
start_reminder_scheduler(app)

if __name__ == "__main__":
    app.run(
        debug=False,
        host=app.config["PAKYAR_HOST"],
        port=app.config["PAKYAR_PORT"],
        use_reloader=False,
    )
