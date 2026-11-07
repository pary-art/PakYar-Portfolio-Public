# core/routes.py

import csv
import hmac
import io
import json
import os
import secrets
import shutil
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    has_request_context,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from core.backups import (
    backup_directory,
    create_backup,
    latest_backup,
    list_backups,
    validate_backup,
)
from core.exports import (
    build_reports_pdf,
    build_reports_xlsx,
    build_supply_pdf,
    build_supply_xlsx,
)
from core.models import (
    ActivityLog,
    ChecklistCompletion,
    ChecklistItem,
    InventorySnapshot,
    ManagerMessage,
    Notification,
    PushSubscription,
    Report,
    ReportResubmission,
    Section,
    ShiftHandoff,
    SupplyInventory,
    SupplyUsage,
    ReminderEvent,
    ReminderRule,
    User,
    db,
)
from core.preferences import (
    DATE_MODE_OPTIONS,
    DEFAULT_PREFERENCES,
    FONT_SCALE_OPTIONS,
    PREFERENCE_THEMES,
)
from core.time_utils import (
    current_jalali_month,
    format_gregorian,
    format_jalali,
    iran_now,
    iran_local_to_utc_naive,
    jalali_month_bounds,
    jalali_month_label,
    jalali_month_value,
    normalize_digits,
    parse_jalali_month,
    utc_now,
)


main = Blueprint("main", __name__)


ALLOWED_ROLES = {"manager", "supervisor", "observer", "staff", "oh_expert"}
# نقش‌های مدیریتی سامانه. ناظر اداره‌کل از نظر سطح دسترسی هم‌سطح
# مسئول خدمات است، اما اختیار تعیین این دو نقش فقط برای ناظر اداره‌کل است.
ELEVATED_ROLES = {"manager", "observer"}  # global visibility, not write permission
ALLOWED_STATUSES = {"pending", "approved", "rejected"}
ALLOWED_SUPPLY_STATUSES = {"pending", "reviewed"}
MESSAGE_STATUSES = {"new", "in_progress", "resolved"}
MESSAGE_PRIORITIES = {"low", "normal", "high", "urgent"}
CHECKLIST_SHIFTS = ("صبح", "عصر", "شب")
CHECKLIST_FREQUENCIES = ("daily", "weekly")
CHECKLIST_FREQUENCY_LABELS = {
    "daily": "روتین روزانه",
    "weekly": "نظافت هفتگی",
}
CHECKLIST_WEEKDAYS = (
    ("saturday", "شنبه"),
    ("sunday", "یکشنبه"),
    ("monday", "دوشنبه"),
    ("tuesday", "سه‌شنبه"),
    ("wednesday", "چهارشنبه"),
    ("thursday", "پنجشنبه"),
    ("friday", "جمعه"),
)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15
DEFAULT_SESSION_IDLE_MINUTES = 30
CHECKLIST_WEEKDAY_LABELS = dict(CHECKLIST_WEEKDAYS)
CHECKLIST_WEEKDAY_CODES = set(CHECKLIST_WEEKDAY_LABELS)
IRAN_WEEKDAY_CODES = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}
INVENTORY_CATEGORIES = {
    "material": "مواد شوینده و مصرفی",
    "equipment": "لوازم و تجهیزات",
}
RESUBMITTED_STATUS = "resubmitted"
CHECKLIST_COMPLETION_STATUSES = {
    "pending",
    "completed",
    "approved",
    "needs_correction",
}
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def user_preference_values(user):
    """Return validated appearance settings for a user, including old records."""
    if not user:
        return dict(DEFAULT_PREFERENCES)

    theme = getattr(user, "theme", None)
    font_scale = getattr(user, "font_scale", None)
    date_mode = getattr(user, "date_mode", None)

    return {
        "theme": theme if theme in PREFERENCE_THEMES else DEFAULT_PREFERENCES["theme"],
        "font_scale": (
            font_scale
            if font_scale in FONT_SCALE_OPTIONS
            else DEFAULT_PREFERENCES["font_scale"]
        ),
        "date_mode": (
            date_mode
            if date_mode in DATE_MODE_OPTIONS
            else DEFAULT_PREFERENCES["date_mode"]
        ),
        "compact_mode": bool(
            getattr(user, "compact_mode", DEFAULT_PREFERENCES["compact_mode"])
        ),
        "reduced_motion": bool(
            getattr(user, "reduced_motion", DEFAULT_PREFERENCES["reduced_motion"])
        ),
    }


def apply_preferences_to_session(user):
    """Keep the current request session in sync with saved preferences."""
    session.update(user_preference_values(user))


def establish_authenticated_session(user):
    """Create a fresh, non-persistent session after successful authentication."""
    session.clear()
    session.permanent = False
    session["session_epoch"] = current_app.config["PAKYAR_SESSION_EPOCH"]
    session["user_id"] = user.id
    session["username"] = user.username
    session["full_name"] = user.full_name
    session["role"] = user.role
    session["last_activity"] = int(time.time())
    apply_preferences_to_session(user)


def csrf_token():
    """Return a per-session token used to protect state-changing forms."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_is_valid():
    supplied = (
        request.form.get("csrf_token", "").strip()
        or request.headers.get("X-CSRFToken", "").strip()
    )
    expected = session.get("_csrf_token", "")
    return bool(expected and supplied) and hmac.compare_digest(
        str(expected),
        str(supplied),
    )


def format_date_for_user(user, value, format_str="%Y/%m/%d %H:%M"):
    """Format a date using the calendar selected by the current user."""
    preferences = user_preference_values(user)
    if preferences["date_mode"] == "gregorian":
        return format_gregorian(value, format_str)
    if preferences["date_mode"] == "both":
        return (
            f"{format_jalali(value, format_str)} | "
            f"{format_gregorian(value, format_str)}"
        )
    return format_jalali(value, format_str)


def get_current_user():
    user_id = session.get("user_id")
    session_epoch = session.get("session_epoch")
    current_epoch = current_app.config.get("PAKYAR_SESSION_EPOCH")

    # نشست‌های قدیمی یا نشست‌های باقی‌مانده از اجرای قبلی برنامه نباید
    # بدون ورود دوباره اجازه دسترسی به داشبورد بدهند.
    if not user_id or not current_epoch or session_epoch != current_epoch:
        if user_id or session_epoch:
            session.clear()
        return None

    user = db.session.get(User, user_id)
    if not user or not getattr(user, "is_active", True):
        session.clear()
        return None

    return user


def session_idle_seconds():
    try:
        minutes = float(
            os.environ.get(
                "PAKYAR_SESSION_IDLE_MINUTES",
                str(DEFAULT_SESSION_IDLE_MINUTES),
            )
        )
    except ValueError:
        minutes = DEFAULT_SESSION_IDLE_MINUTES

    return max(minutes, 5) * 60


@main.before_request
def enforce_session_timeout():
    """Expire inactive sessions before protected routes are rendered."""
    if request.endpoint in {"main.login", "main.health"}:
        return None

    user = get_current_user()
    if not user:
        return None

    now = int(time.time())
    last_activity = session.get("last_activity")
    if last_activity and now - int(last_activity) > session_idle_seconds():
        session.clear()
        flash("به‌دلیل بی‌استفاده‌بودن، از حساب خارج شدید.", "warning")
        return redirect(url_for("main.login"))

    session["last_activity"] = now
    last_seen = getattr(user, "last_seen_at", None)
    if not last_seen or (utc_now() - last_seen).total_seconds() >= 60:
        user.last_seen_at = utc_now()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return None


@main.before_request
def protect_state_changing_requests():
    """Reject forged state-changing requests before a route is executed."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    # Login has no authenticated session yet; all authenticated forms are
    # protected by the per-session token.
    if request.endpoint == "main.login":
        return None

    if csrf_is_valid():
        return None

    current_app.logger.warning(
        "CSRF validation failed for %s from %s",
        request.path,
        request.remote_addr or "unknown",
    )
    flash("نشست فرم منقضی شده است؛ صفحه را دوباره باز کنید و دوباره تلاش کنید.", "warning")
    return redirect(url_for("main.login"))


def password_is_hashed(password):
    if not password:
        return False

    return password.startswith(
        (
            "pbkdf2:",
            "scrypt:",
            "argon2:",
        )
    )


def verify_password(user, password):
    if not user or not user.password:
        return False

    if password_is_hashed(user.password):
        return check_password_hash(user.password, password)

    return user.password == password


def commit_changes():
    try:
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def log_activity(action, details=None, user_id=None):
    user = get_current_user()
    request_context = has_request_context()

    activity = ActivityLog(
        user_id=user_id if user_id is not None else (user.id if user else None),
        action=action,
        details=details,
        ip_address=(request.remote_addr or "")[:64]
        if request_context
        else None,
        user_agent=(request.user_agent.string or "")[:500]
        if request_context
        else None,
    )

    db.session.add(activity)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def dashboard_redirect(user=None):
    user = user or get_current_user()

    if not user:
        return redirect(url_for("main.login"))

    if user.role == "oh_expert":
        return redirect(url_for("workload.expert_people"))
    if user.role == "observer":
        return redirect(url_for("main.observer"))

    if user.role == "manager":
        if user.is_appointed_manager:
            return redirect(url_for("main.manager"))
        if not active_observer_exists():
            return redirect(url_for("main.users"))
        return redirect(url_for("main.manager_assignment_pending"))

    if user.role == "supervisor":
        return redirect(url_for("main.supervisor"))

    return redirect(url_for("main.staff"))


def is_resubmitted_report(report):
    """Return whether this report is a replacement created by resubmission."""
    return (
        report.status == RESUBMITTED_STATUS
        or getattr(report, "resubmission_source", None) is not None
    )


def resubmission_status(report):
    if not is_resubmitted_report(report):
        return report.status

    if report.status == "approved":
        return "resubmitted_approved"
    if report.status == "rejected":
        return "resubmitted_rejected"
    return "resubmitted_pending"


def report_status_label(report):
    labels = {
        "pending": "در انتظار بررسی",
        "approved": "تأیید شده",
        "rejected": "رد شده",
        RESUBMITTED_STATUS: "ثبت مجدد شده",
        "resubmitted_pending": "در انتظار بررسی پس از ثبت مجدد",
        "resubmitted_approved": "تأیید پس از ثبت مجدد",
        "resubmitted_rejected": "رد پس از ثبت مجدد",
    }
    return labels.get(resubmission_status(report), report.status or "")


def report_stats(reports):
    """Return current statuses plus the complete resubmission history."""
    resubmitted_reports = [
        report for report in reports if is_resubmitted_report(report)
    ]
    return {
        "pending": sum(report.status == "pending" for report in reports),
        "approved": sum(report.status == "approved" for report in reports),
        "rejected": sum(report.status == "rejected" for report in reports),
        "resubmitted": len(resubmitted_reports),
        "resubmitted_approved": sum(
            report.status == "approved" for report in resubmitted_reports
        ),
        "resubmitted_pending": sum(
            resubmission_status(report) == "resubmitted_pending"
            for report in resubmitted_reports
        ),
        "resubmitted_rejected": sum(
            report.status == "rejected" for report in resubmitted_reports
        ),
    }


def percentage(value, total):
    return round((value / total) * 100, 1) if total else 0


def visible_reports_for(user):
    if user.role in {"manager", "observer"}:
        return Report.query.order_by(Report.created_at.desc()).all()

    section_ids = [section.id for section in user.sections]
    if not section_ids:
        return []

    return (
        Report.query
        .filter(Report.section_id.in_(section_ids))
        .order_by(Report.created_at.desc())
        .all()
    )


def visible_supply_usages_for(user):
    if user.role in {"manager", "observer"}:
        return (
            SupplyUsage.query
            .order_by(SupplyUsage.created_at.desc())
            .all()
        )

    section_ids = [section.id for section in user.sections]
    if not section_ids:
        return []

    return (
        SupplyUsage.query
        .filter(SupplyUsage.section_id.in_(section_ids))
        .order_by(SupplyUsage.created_at.desc())
        .all()
    )


def visible_inventory_for(user):
    """Return active inventory items for the user's allowed sections."""
    if user.role in {"manager", "observer"}:
        return (
            SupplyInventory.query
            .filter(SupplyInventory.is_active.is_(True))
            .order_by(
                SupplyInventory.section_id.asc(),
                SupplyInventory.category.asc(),
                SupplyInventory.name.asc(),
            )
            .all()
        )

    section_ids = [section.id for section in user.sections]
    if not section_ids:
        return []

    return (
        SupplyInventory.query
        .filter(
            SupplyInventory.section_id.in_(section_ids),
            SupplyInventory.is_active.is_(True),
        )
        .order_by(
            SupplyInventory.section_id.asc(),
            SupplyInventory.category.asc(),
            SupplyInventory.name.asc(),
        )
        .all()
    )


def inventory_alerts_for(user):
    """Return low-stock items; this view is intentionally supervisor-only."""
    if user.role != "supervisor":
        return []

    alerts = []
    for item in visible_inventory_for(user):
        if item.current_quantity <= item.minimum_quantity:
            alerts.append(
                {
                    "item": item,
                    "shortage": max(
                        item.minimum_quantity - item.current_quantity,
                        0,
                    ),
                    "severity": (
                        "critical"
                        if item.current_quantity <= 0
                        else "warning"
                    ),
                }
            )

    return sorted(
        alerts,
        key=lambda alert: (
            0 if alert["severity"] == "critical" else 1,
            alert["item"].section.name,
            alert["item"].name,
        ),
    )


def visible_messages_for(user):
    """Return only the messages the current user is allowed to see."""
    if user.role == "observer":
        return ManagerMessage.query.order_by(ManagerMessage.created_at.desc()).all()

    if user.role == "manager":
        return (
            ManagerMessage.query
            .filter_by(sender_id=user.id)
            .order_by(ManagerMessage.created_at.desc())
            .all()
        )

    return (
        ManagerMessage.query
        .filter_by(recipient_id=user.id)
        .order_by(ManagerMessage.created_at.desc())
        .all()
    )


def reminder_assigned_ids(rule):
    try:
        values = json.loads(rule.assigned_user_ids or "[]")
    except (TypeError, ValueError):
        values = []
    return {int(value) for value in values if str(value).isdigit()}


def role_label(role):
    return {
        "manager": "مسئول خدمات",
        "supervisor": "مسئول بخش",
        "observer": "ناظر اداره‌کل",
        "staff": "پرسنل",
        "oh_expert": "کارشناس بهداشت حرفه‌ای",
        "physician": "پزشک طب کار (نقش قدیمی و غیرفعال)",
    }.get(role, role or "-")


def active_observer_exists():
    """Return whether the system has at least one active global observer."""
    return (
        User.query
        .filter(
            User.role == "observer",
            User.is_active.is_(True),
        )
        .first()
        is not None
    )


def manager_bootstrap_allowed(user):
    """Allow the legacy first manager to create the system's first observer only."""
    return bool(
        user
        and user.role == "manager"
        and not user.is_appointed_manager
        and not active_observer_exists()
        and request.endpoint in {"main.users", "main.add_user"}
    )


def visible_reminder_rules_for(user):
    if user.role in {"manager", "observer"}:
        return ReminderRule.query.order_by(ReminderRule.created_at.desc()).all()
    section_ids = {section.id for section in user.sections}
    if not section_ids:
        return []
    return ReminderRule.query.filter(ReminderRule.section_id.in_(section_ids)).order_by(ReminderRule.created_at.desc()).all()


def visible_notifications_for(user, unread_only=False, limit=None):
    if not user:
        return []

    query = (
        Notification.query
        .filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc())
    )
    if unread_only:
        query = query.filter_by(is_read=False)
    if limit:
        query = query.limit(limit)
    return query.all()


def create_notifications(
    user_ids,
    title,
    body,
    notification_type="info",
    target_url=None,
):
    unique_ids = {
        int(user_id)
        for user_id in user_ids
        if user_id is not None
    }
    if not unique_ids:
        return

    for user_id in unique_ids:
        db.session.add(
            Notification(
                user_id=user_id,
                title=title,
                body=body,
                notification_type=notification_type,
                target_url=target_url,
                is_read=False,
            )
        )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def notify_managers(
    title,
    body,
    notification_type="info",
    target_url=None,
    exclude_ids=None,
):
    excluded = set(exclude_ids or ())
    manager_ids = [
        candidate.id
        for candidate in User.query.filter(
            User.role.in_(tuple(ELEVATED_ROLES)),
            User.is_active.is_(True),
        ).all()
        if candidate.id not in excluded
        and (
            candidate.role == "observer"
            or candidate.is_appointed_manager
        )
    ]
    create_notifications(
        manager_ids,
        title,
        body,
        notification_type,
        target_url,
    )


def notify_section_reviewers(
    section_id,
    title,
    body,
    notification_type="info",
    target_url=None,
    exclude_ids=None,
):
    excluded = set(exclude_ids or ())
    reviewers = User.query.filter(
        User.role.in_(("manager", "observer", "supervisor")),
        User.is_active.is_(True),
    ).all()
    reviewer_ids = []
    for reviewer in reviewers:
        has_access = (
            reviewer.role == "observer"
            or (
                reviewer.role == "manager"
                and reviewer.is_appointed_manager
            )
            or any(
            section.id == section_id
            for section in reviewer.sections
            )
        )
        if has_access and reviewer.id not in excluded:
            reviewer_ids.append(reviewer.id)

    create_notifications(
        reviewer_ids,
        title,
        body,
        notification_type,
        target_url,
    )


def message_status_label(status):
    return {
        "new": "جدید",
        "in_progress": "در حال پیگیری",
        "resolved": "انجام شد",
    }.get(status, status or "-")


def message_priority_label(priority):
    return {
        "low": "کم",
        "normal": "عادی",
        "high": "مهم",
        "urgent": "فوری",
    }.get(priority, priority or "-")


def visible_checklist_sections_for(user):
    if user.role in {"manager", "observer"}:
        return Section.query.order_by(Section.name.asc()).all()

    return sorted(user.sections, key=lambda section: section.name)


def current_checklist_weekday(checklist_date=None):
    checklist_date = checklist_date or iran_now().date()
    return IRAN_WEEKDAY_CODES[checklist_date.weekday()]


def checklist_period_key(frequency, checklist_date=None):
    checklist_date = checklist_date or iran_now().date()

    if frequency == "weekly":
        # هفتهٔ کاری سامانه از شنبه تا جمعه محاسبه می‌شود.
        days_since_saturday = (checklist_date.weekday() - 5) % 7
        week_start = checklist_date - timedelta(days=days_since_saturday)
        return week_start.isoformat()

    return checklist_date.isoformat()


def checklist_period_label(frequency, weekday=None, shift=None):
    if frequency == "weekly":
        weekday_label = CHECKLIST_WEEKDAY_LABELS.get(weekday, "روز انتخاب‌شده")
        shift_label = shift or "شیفت انتخاب‌شده"
        return f"{weekday_label} · شیفت {shift_label} · هفتهٔ جاری"
    return "امروز؛ هر شیفت یک‌بار"


def checklist_redirect_url(
    section_id=None,
    frequency="daily",
    shift="صبح",
    weekday=None,
):
    values = {"frequency": frequency}
    if section_id:
        values["section_id"] = section_id
    if shift:
        values["shift"] = shift
    if frequency == "weekly" and weekday:
        values["weekday"] = weekday
    return url_for("main.checklists", **values)


def checklist_items_for(
    section_id,
    frequency="daily",
    shift="صبح",
    weekday=None,
):
    if not section_id or frequency not in CHECKLIST_FREQUENCIES:
        return []

    if shift not in CHECKLIST_SHIFTS:
        return []

    query = (
        ChecklistItem.query
        .filter_by(
            section_id=section_id,
            frequency=frequency,
            shift=shift,
            is_active=True,
        )
    )

    if frequency == "daily":
        query = query.filter(ChecklistItem.weekday.is_(None))
    else:
        if weekday not in CHECKLIST_WEEKDAY_CODES:
            return []
        query = query.filter(ChecklistItem.weekday == weekday)

    return query.order_by(
        ChecklistItem.sort_order.asc(),
        ChecklistItem.id.asc(),
    ).all()


def checklist_completion_map(
    items,
    user_id,
    checklist_date,
    period_key,
    shift,
):
    item_ids = [item.id for item in items]
    if not item_ids:
        return {}

    completions = (
        ChecklistCompletion.query
        .filter(
            ChecklistCompletion.item_id.in_(item_ids),
            ChecklistCompletion.user_id == user_id,
            ChecklistCompletion.period_key == period_key,
            ChecklistCompletion.shift == shift,
        )
        .all()
    )
    return {completion.item_id: completion for completion in completions}


def checklist_completion_is_done(completion):
    """Return whether a checklist item is accepted as complete."""
    return bool(
        completion
        and completion.is_completed
        and getattr(completion, "status", "completed") != "needs_correction"
    )


def checklist_completion_status_label(status):
    return {
        "pending": "ثبت نشده",
        "completed": "ثبت شده؛ در انتظار بررسی",
        "approved": "تأیید شده",
        "needs_correction": "نیازمند اصلاح",
    }.get(status or "pending", status or "ثبت نشده")


def visible_shift_handoffs_for(user, section_id=None, limit=80):
    """Return handoff notes inside the current user's allowed scope."""
    query = ShiftHandoff.query
    if section_id:
        query = query.filter_by(section_id=section_id)

    if user.role in ELEVATED_ROLES:
        pass
    elif user.role == "supervisor":
        section_ids = [section.id for section in user.sections]
        if not section_ids:
            return []
        query = query.filter(ShiftHandoff.section_id.in_(section_ids))
    else:
        query = query.filter_by(user_id=user.id)

    return (
        query
        .order_by(
            ShiftHandoff.handoff_date.desc(),
            ShiftHandoff.created_at.desc(),
        )
        .limit(limit)
        .all()
    )


def checklist_review_completions(
    user,
    section_id=None,
    frequency=None,
    shift=None,
    weekday=None,
    limit=100,
):
    """Return submitted checklist entries that need supervisor attention."""
    query = ChecklistCompletion.query.join(ChecklistItem)

    if user.role == "supervisor":
        section_ids = [section.id for section in user.sections]
        if not section_ids:
            return []
        query = query.filter(ChecklistItem.section_id.in_(section_ids))
    elif user.role not in ELEVATED_ROLES:
        return []

    if section_id:
        query = query.filter(ChecklistItem.section_id == section_id)
    if frequency in CHECKLIST_FREQUENCIES:
        query = query.filter(ChecklistItem.frequency == frequency)
    if shift in CHECKLIST_SHIFTS:
        query = query.filter(ChecklistCompletion.shift == shift)
    if frequency == "weekly" and weekday in CHECKLIST_WEEKDAY_CODES:
        query = query.filter(ChecklistItem.weekday == weekday)

    return (
        query
        .filter(
            ChecklistCompletion.is_completed.is_(True),
            ChecklistCompletion.status.in_(
                ("completed", "needs_correction", "approved")
            ),
        )
        .order_by(ChecklistCompletion.updated_at.desc())
        .limit(limit)
        .all()
    )


def inventory_forecast_for(user):
    """Estimate days remaining from inventory snapshots, per visible item."""
    forecasts = []
    for item in visible_inventory_for(user):
        snapshots = sorted(
            getattr(item, "snapshots", []) or [],
            key=lambda snapshot: snapshot.recorded_at or utc_now(),
        )
        daily_average = 0.0
        data_days = 0.0
        if len(snapshots) >= 2:
            first = snapshots[0]
            last = snapshots[-1]
            if first.recorded_at and last.recorded_at:
                data_days = max(
                    (last.recorded_at - first.recorded_at).total_seconds()
                    / 86400,
                    0,
                )
                if data_days >= 1:
                    decrease = max(
                        (first.quantity or 0) - (last.quantity or 0),
                        0,
                    )
                    daily_average = decrease / data_days

        days_remaining = (
            round(item.current_quantity / daily_average, 1)
            if daily_average > 0
            else None
        )
        if item.current_quantity <= 0:
            forecast_status = "critical"
            forecast_label = "تمام شده"
        elif item.current_quantity <= item.minimum_quantity:
            forecast_status = "warning"
            forecast_label = "زیر حد هشدار"
        elif days_remaining is not None and days_remaining <= 7:
            forecast_status = "forecast"
            forecast_label = "احتمال کمبود تا ۷ روز"
        elif days_remaining is not None and days_remaining <= 14:
            forecast_status = "soon"
            forecast_label = "احتمال کمبود تا ۱۴ روز"
        else:
            forecast_status = "normal"
            forecast_label = "مناسب"

        forecasts.append(
            {
                "item": item,
                "daily_average": round(daily_average, 2),
                "days_remaining": days_remaining,
                "data_days": round(data_days, 1),
                "status": forecast_status,
                "label": forecast_label,
                "enough_data": data_days >= 1 and daily_average > 0,
            }
        )

    return sorted(
        forecasts,
        key=lambda row: (
            {"critical": 0, "warning": 1, "forecast": 2, "soon": 3, "normal": 4}
            .get(row["status"], 5),
            row["item"].section.name if row["item"].section else "",
            row["item"].name,
        ),
    )


def staff_for_section(section_id):
    staff_users = (
        User.query
        .filter_by(role="staff", is_active=True)
        .order_by(User.full_name.asc())
        .all()
    )
    return [
        staff
        for staff in staff_users
        if section_id in {section.id for section in staff.sections}
    ]


def checklist_observation(
    section,
    frequency,
    shift,
    weekday,
    checklist_date,
    period_key,
):
    """Build the current period completion summary for a section."""
    items = checklist_items_for(section.id, frequency, shift, weekday)
    staff_users = staff_for_section(section.id)
    item_ids = [item.id for item in items]
    staff_ids = [staff.id for staff in staff_users]

    completions = []
    if item_ids and staff_ids:
        completions = (
            ChecklistCompletion.query
            .filter(
                ChecklistCompletion.item_id.in_(item_ids),
                ChecklistCompletion.user_id.in_(staff_ids),
                ChecklistCompletion.period_key == period_key,
                ChecklistCompletion.shift == shift,
            )
            .all()
        )

    completion_lookup = {
        (completion.user_id, completion.item_id): completion
        for completion in completions
    }
    observations = []

    for staff in staff_users:
        staff_completions = [
            completion_lookup.get((staff.id, item.id))
            for item in items
        ]
        completed_count = sum(
            checklist_completion_is_done(completion)
            for completion in staff_completions
        )
        correction_count = sum(
            bool(
                completion
                and completion.is_completed
                and completion.status == "needs_correction"
            )
            for completion in staff_completions
        )
        completed_times = [
            completion.completed_at
            for completion in staff_completions
            if completion and completion.is_completed and completion.completed_at
        ]
        observations.append(
            {
                "staff": staff,
                "completed": completed_count,
                "total": len(items),
                "rate": percentage(completed_count, len(items)),
                "corrections": correction_count,
                "last_completed_at": max(completed_times)
                if completed_times
                else None,
            }
        )

    item_observations = []
    for item in items:
        completed_by = [
            staff.full_name
            for staff in staff_users
                if (
                    completion_lookup.get((staff.id, item.id))
                    and checklist_completion_is_done(
                        completion_lookup[(staff.id, item.id)]
                    )
                )
        ]
        completed_staff_ids = {
            staff.id
            for staff in staff_users
                if (
                    completion_lookup.get((staff.id, item.id))
                    and checklist_completion_is_done(
                        completion_lookup[(staff.id, item.id)]
                    )
                )
        }
        pending_by = [
            staff.full_name
            for staff in staff_users
            if staff.id not in completed_staff_ids
        ]
        item_observations.append(
            {
                "item": item,
                "completed_by": completed_by,
                "pending_by": pending_by,
            }
        )

    total_checks = len(items) * len(staff_users)
    completed_checks = sum(
        observation["completed"] for observation in observations
    )
    correction_checks = sum(
        observation["corrections"] for observation in observations
    )
    stats = {
        "items": len(items),
        "staff": len(staff_users),
        "completed_checks": completed_checks,
        "total_checks": total_checks,
        "rate": percentage(completed_checks, total_checks),
        "corrections": correction_checks,
    }

    return items, staff_users, observations, item_observations, stats


def checklist_board_for(user):
    """Build the checklist workboard for today's sections and schedules."""
    checklist_date = iran_now().date()
    weekday = current_checklist_weekday(checklist_date)
    weekday_label = CHECKLIST_WEEKDAY_LABELS[weekday]
    daily_period = checklist_period_key("daily", checklist_date)
    weekly_period = checklist_period_key("weekly", checklist_date)
    sections = visible_checklist_sections_for(user)
    board = []
    total_checks = 0
    completed_checks = 0
    pending_checks = 0
    pending_cards = 0
    unassigned_cards = 0

    for section in sections:
        if user.role == "staff":
            assigned_staff = [user]
        else:
            assigned_staff = staff_for_section(section.id)

        staff_ids = [staff.id for staff in assigned_staff]

        for shift in CHECKLIST_SHIFTS:
            daily_items = checklist_items_for(
                section.id,
                "daily",
                shift,
            )
            weekly_items = checklist_items_for(
                section.id,
                "weekly",
                shift,
                weekday,
            )
            scheduled_items = [
                ("daily", item, daily_period)
                for item in daily_items
            ] + [
                ("weekly", item, weekly_period)
                for item in weekly_items
            ]

            if not scheduled_items:
                continue

            item_ids = [item.id for _, item, _ in scheduled_items]
            period_keys = {period for _, _, period in scheduled_items}
            completions = []
            if staff_ids:
                completions = (
                    ChecklistCompletion.query
                    .filter(
                        ChecklistCompletion.item_id.in_(item_ids),
                        ChecklistCompletion.user_id.in_(staff_ids),
                        ChecklistCompletion.period_key.in_(period_keys),
                        ChecklistCompletion.shift == shift,
                    )
                    .all()
                )

            completion_lookup = {
                (
                    completion.user_id,
                    completion.item_id,
                    completion.period_key,
                ): completion
                for completion in completions
            }
            pending_items = []
            card_completed = 0

            for frequency, item, period_key in scheduled_items:
                missing_people = []
                for staff in assigned_staff:
                    completion = completion_lookup.get(
                        (staff.id, item.id, period_key)
                    )
                    if checklist_completion_is_done(completion):
                        card_completed += 1
                    else:
                        if (
                            completion
                            and completion.status == "needs_correction"
                        ):
                            pending_items.append(
                                {
                                    "title": item.title,
                                    "frequency": frequency,
                                    "frequency_label": (
                                        CHECKLIST_FREQUENCY_LABELS[frequency]
                                        + " · اصلاح لازم"
                                    ),
                                    "pending_people": [staff.full_name],
                                }
                            )
                            continue
                        missing_people.append(staff.full_name)

                if missing_people:
                    pending_items.append(
                        {
                            "title": item.title,
                            "frequency": frequency,
                            "frequency_label": CHECKLIST_FREQUENCY_LABELS[frequency],
                            "pending_people": missing_people,
                        }
                    )

            card_total = len(scheduled_items) * len(assigned_staff)
            card_pending = card_total - card_completed
            is_unassigned = not assigned_staff
            if is_unassigned:
                unassigned_cards += 1
            elif card_pending:
                pending_cards += 1

            total_checks += card_total
            completed_checks += card_completed
            pending_checks += card_pending
            board.append(
                {
                    "section": section,
                    "shift": shift,
                    "weekday": weekday,
                    "weekday_label": weekday_label,
                    "daily_count": len(daily_items),
                    "weekly_count": len(weekly_items),
                    "total_items": len(scheduled_items),
                    "staff_count": len(assigned_staff),
                    "completed_checks": card_completed,
                    "total_checks": card_total,
                    "pending_checks": card_pending,
                    "rate": percentage(card_completed, card_total),
                    "pending_items": pending_items[:5],
                    "pending_items_total": len(pending_items),
                    "is_unassigned": is_unassigned,
                    "status": (
                        "unassigned"
                        if is_unassigned
                        else "complete"
                        if not card_pending
                        else "pending"
                    ),
                    "daily_url": url_for(
                        "main.checklists",
                        section_id=section.id,
                        frequency="daily",
                        shift=shift,
                    ),
                    "weekly_url": url_for(
                        "main.checklists",
                        section_id=section.id,
                        frequency="weekly",
                        shift=shift,
                        weekday=weekday,
                    ),
                }
            )

    return {
        "date": checklist_date,
        "date_label": format_date_for_user(user, iran_now(), "%Y/%m/%d"),
        "weekday": weekday,
        "weekday_label": weekday_label,
        "cards": board,
        "total_cards": len(board),
        "pending_cards": pending_cards,
        "unassigned_cards": unassigned_cards,
        "complete_cards": len(board) - pending_cards - unassigned_cards,
        "total_checks": total_checks,
        "completed_checks": completed_checks,
        "pending_checks": pending_checks,
        "rate": percentage(completed_checks, total_checks),
    }


def followup_board_for(user, today_board=None, inventory_alerts=None):
    """Build the live follow-up board for managers and supervisors."""
    empty_board = {
        "report_items": [],
        "checklist_cards": [],
        "supply_usages": [],
        "messages": [],
        "inventory_alerts": [],
        "handoffs": [],
        "summary": {
            "total": 0,
            "reports": 0,
            "checklists": 0,
            "supplies": 0,
            "messages": 0,
            "inventory": 0,
            "handoffs": 0,
        },
    }

    if user.role not in {"manager", "observer", "supervisor"}:
        return empty_board

    reports = visible_reports_for(user)
    report_items = []
    for report in reports:
        if report.status in {"pending", RESUBMITTED_STATUS}:
            is_resubmitted = is_resubmitted_report(report)
            report_items.append(
                {
                    "report": report,
                    "title": (
                        "گزارش ثبت‌مجددشده؛ نیازمند بررسی"
                        if is_resubmitted
                        else "گزارش نظافت؛ نیازمند بررسی"
                    ),
                    "status": "resubmitted" if is_resubmitted else "pending",
                    "status_label": report_status_label(report),
                    "priority": "high" if is_resubmitted else "normal",
                    "row_class": (
                        "table-primary" if is_resubmitted else "table-warning"
                    ),
                }
            )
        elif report.status == "rejected" and not report.resubmissions:
            report_items.append(
                {
                    "report": report,
                    "title": "گزارش ردشده؛ منتظر اصلاح پرسنل",
                    "status": "rejected",
                    "status_label": "منتظر ثبت مجدد",
                    "priority": "high",
                    "row_class": "table-danger",
                }
            )

    if today_board is None:
        today_board = checklist_board_for(user)
    checklist_cards = [
        card
        for card in today_board["cards"]
        if card["status"] != "complete"
    ]

    supply_usages = [
        usage
        for usage in visible_supply_usages_for(user)
        if usage.status == "pending"
    ]

    messages = [
        message
        for message in visible_messages_for(user)
        if message.status != "resolved"
    ]

    handoffs = [
        handoff
        for handoff in visible_shift_handoffs_for(user)
        if not handoff.is_acknowledged
    ]

    if inventory_alerts is None:
        inventory_alerts = inventory_alerts_for(user)
    else:
        inventory_alerts = list(inventory_alerts)

    summary = {
        "total": (
            len(report_items)
            + len(checklist_cards)
            + len(supply_usages)
            + len(messages)
            + len(inventory_alerts)
            + len(handoffs)
        ),
        "reports": len(report_items),
        "checklists": len(checklist_cards),
        "supplies": len(supply_usages),
        "messages": len(messages),
        "inventory": len(inventory_alerts),
        "handoffs": len(handoffs),
    }

    return {
        "report_items": report_items,
        "checklist_cards": checklist_cards,
        "supply_usages": supply_usages,
        "messages": messages,
        "inventory_alerts": inventory_alerts,
        "handoffs": handoffs,
        "summary": summary,
        "today_board": today_board,
    }


def selected_month_range(value):
    current_jy, current_jm = current_jalali_month()
    fallback = jalali_month_value(current_jy, current_jm)
    requested = normalize_digits((value or "").strip()).replace("/", "-")

    parsed = parse_jalali_month(requested)
    if parsed:
        jy, jm = parsed
        start, end = jalali_month_bounds(jy, jm)
        return (
            jalali_month_value(jy, jm),
            start,
            end,
            jalali_month_label(jy, jm),
        )

    # پشتیبانی از لینک‌های قدیمی که ماه را میلادی ارسال می‌کردند.
    try:
        legacy_start = datetime.strptime(requested, "%Y-%m")
        if 1900 <= legacy_start.year <= 2200:
            if legacy_start.month == 12:
                legacy_end = legacy_start.replace(
                    year=legacy_start.year + 1,
                    month=1,
                )
            else:
                legacy_end = legacy_start.replace(
                    month=legacy_start.month + 1,
                )

            return (
                requested,
                iran_local_to_utc_naive(legacy_start),
                iran_local_to_utc_naive(legacy_end),
                f"ماه میلادی {requested}",
            )
    except ValueError:
        pass

    start, end = jalali_month_bounds(current_jy, current_jm)
    return (
        fallback,
        start,
        end,
        jalali_month_label(current_jy, current_jm),
    )


def count_usage_items(value):
    if not value:
        return 0
    return sum(bool(line.strip()) for line in value.splitlines())


def supply_section_summary(usages, sections):
    result = []
    for section in sections:
        section_usages = [
            usage
            for usage in usages
            if usage.section_id == section.id
        ]
        result.append(
            {
                "name": section.name,
                "total": len(section_usages),
                "materials": sum(
                    count_usage_items(usage.materials)
                    for usage in section_usages
                ),
                "equipment": sum(
                    count_usage_items(usage.equipment)
                    for usage in section_usages
                ),
                "cost": sum(
                    parse_non_negative_quantity(
                        str(getattr(usage, "estimated_cost", 0) or 0)
                    )
                    or 0
                    for usage in section_usages
                ),
                "reviewed": sum(
                    usage.status == "reviewed"
                    for usage in section_usages
                ),
                "pending": sum(
                    usage.status == "pending"
                    for usage in section_usages
                ),
            }
        )
    return result


def manager_dashboard_data(
    user,
    reports,
    today_board,
    month_value=None,
    section_id=None,
    shift=None,
):
    month_value, start, end, month_label = selected_month_range(month_value or "")
    month_reports = [
        report
        for report in reports
        if report.created_at and start <= report.created_at < end
    ]
    if section_id:
        month_reports = [
            report
            for report in month_reports
            if report.section_id == section_id
        ]
    if shift:
        month_reports = [
            report
            for report in month_reports
            if report.shift == shift
        ]
    month_usages = [
        usage
        for usage in visible_supply_usages_for(user)
        if usage.created_at and start <= usage.created_at < end
    ]
    if section_id:
        month_usages = [
            usage
            for usage in month_usages
            if usage.section_id == section_id
        ]
    if shift:
        month_usages = [
            usage
            for usage in month_usages
            if usage.shift == shift
        ]
    sections = (
        Section.query.order_by(Section.name.asc()).all()
        if user.role in {"manager", "observer"}
        else sorted(user.sections, key=lambda section: section.name)
    )
    if section_id:
        sections = [section for section in sections if section.id == section_id]
    month_report_stats = report_stats(month_reports)
    section_data = supply_section_summary(month_usages, sections)
    staff_performance = staff_performance_data_for(user, month_reports)
    inventory_forecasts = inventory_forecast_for(user)
    report_by_section = []
    for section in sections:
        section_reports = [
            report
            for report in month_reports
            if report.section_id == section.id
        ]
        report_by_section.append(
            {
                "name": section.name,
                "total": len(section_reports),
                "approved": sum(
                    report.status == "approved"
                    for report in section_reports
                ),
                "rejected": sum(
                    report.status == "rejected"
                    for report in section_reports
                ),
            }
        )

    return {
        "month_value": month_value,
        "month_label": month_label,
        "report_total": len(month_reports),
        "approved_rate": percentage(
            month_report_stats["approved"],
            len(month_reports),
        ),
        "rejected_rate": percentage(
            month_report_stats["rejected"],
            len(month_reports),
        ),
        "resubmitted_rate": percentage(
            month_report_stats["resubmitted"],
            len(month_reports),
        ),
        "pending_rate": percentage(
            month_report_stats["pending"],
            len(month_reports),
        ),
        "approved_count": month_report_stats["approved"],
        "rejected_count": month_report_stats["rejected"],
        "resubmitted_count": month_report_stats["resubmitted"],
        "pending_count": month_report_stats["pending"],
        "checklist_rate": today_board["rate"],
        "supply_total": len(month_usages),
        "supply_cost": sum(
            item["cost"]
            for item in section_data
        ),
        "section_data": section_data,
        "report_by_section": report_by_section,
        "top_staff": sorted(
            staff_performance,
            key=lambda item: (
                -item["quality_score"],
                -item["total"],
                item["name"],
            ),
        )[:5],
        "inventory_forecast_alerts": sum(
            item["status"] in {"critical", "warning", "forecast", "soon"}
            for item in inventory_forecasts
        ),
        "latest_backup": latest_backup(current_app),
        "backup_count": len(list_backups(current_app)),
    }


def staff_performance_data_for(user, reports=None):
    """Build one consistent performance dataset for manager and supervisor views."""
    reports = visible_reports_for(user) if reports is None else reports
    if user.role in ELEVATED_ROLES:
        staff_users = (
            User.query
            .filter_by(role="staff", is_active=True)
            .order_by(User.full_name.asc())
            .all()
        )
    elif user.role == "supervisor":
        section_ids = {section.id for section in user.sections}
        staff_users = [
            staff
            for staff in (
                User.query
                .filter_by(role="staff", is_active=True)
                .order_by(User.full_name.asc())
                .all()
            )
            if any(section.id in section_ids for section in staff.sections)
        ]
    else:
        return []

    staff_ids = [staff.id for staff in staff_users]
    checklist_rows = (
        ChecklistCompletion.query
        .filter(ChecklistCompletion.user_id.in_(staff_ids))
        .all()
        if staff_ids
        else []
    )
    checklist_by_user = {}
    for row in checklist_rows:
        checklist_by_user.setdefault(row.user_id, []).append(row)

    supply_rows = (
        SupplyUsage.query
        .filter(SupplyUsage.user_id.in_(staff_ids))
        .all()
        if staff_ids
        else []
    )
    supply_by_user = {}
    for row in supply_rows:
        supply_by_user.setdefault(row.user_id, []).append(row)

    result = []
    for staff in staff_users:
        staff_reports = [
            report for report in reports if report.user_id == staff.id
        ]
        total = len(staff_reports)
        approved = sum(report.status == "approved" for report in staff_reports)
        rejected = sum(report.status == "rejected" for report in staff_reports)
        pending = sum(report.status == "pending" for report in staff_reports)
        resubmitted_reports = [
            report for report in staff_reports if is_resubmitted_report(report)
        ]
        resubmitted = len(resubmitted_reports)
        resubmitted_approved = sum(
            report.status == "approved" for report in resubmitted_reports
        )
        resubmitted_pending = sum(
            resubmission_status(report) == "resubmitted_pending"
            for report in resubmitted_reports
        )
        resubmitted_rejected = sum(
            report.status == "rejected" for report in resubmitted_reports
        )
        regular_approved = approved - resubmitted_approved
        regular_rejected = rejected - resubmitted_rejected
        regular_pending = pending - resubmitted_pending

        staff_checklists = checklist_by_user.get(staff.id, [])
        checklist_total = len(staff_checklists)
        checklist_completed = sum(
            checklist_completion_is_done(row)
            for row in staff_checklists
        )
        checklist_submitted = sum(
            bool(row.is_completed) for row in staff_checklists
        )
        corrections = sum(
            row.status == "needs_correction" for row in staff_checklists
        )
        checklist_rate = percentage(checklist_completed, checklist_total)

        available_quality = [(checklist_rate, 0.45)]
        if total:
            available_quality.extend(
                [
                    (percentage(approved, total), 0.4),
                    (100 - percentage(rejected, total), 0.15),
                ]
            )
        quality_weight = sum(weight for _value, weight in available_quality)
        quality_score = round(
            sum(value * weight for value, weight in available_quality)
            / quality_weight,
            1,
        ) if quality_weight else 0

        staff_supply = supply_by_user.get(staff.id, [])
        result.append(
            {
                "id": staff.id,
                "name": staff.full_name,
                "total": total,
                "approved": approved,
                "rejected": rejected,
                "pending": pending,
                "resubmitted": resubmitted,
                "resubmitted_approved": resubmitted_approved,
                "resubmitted_pending": resubmitted_pending,
                "resubmitted_rejected": resubmitted_rejected,
                "regular_approved": regular_approved,
                "regular_rejected": regular_rejected,
                "regular_pending": regular_pending,
                "approved_rate": percentage(approved, total),
                "rejected_rate": percentage(rejected, total),
                "pending_rate": percentage(pending, total),
                "resubmitted_rate": percentage(resubmitted, total),
                "resubmitted_approved_rate": percentage(
                    resubmitted_approved,
                    total,
                ),
                "regular_approved_rate": percentage(regular_approved, total),
                "regular_rejected_rate": percentage(regular_rejected, total),
                "regular_pending_rate": percentage(regular_pending, total),
                "resubmitted_pending_rate": percentage(
                    resubmitted_pending,
                    total,
                ),
                "resubmitted_rejected_rate": percentage(
                    resubmitted_rejected,
                    total,
                ),
                "checklist_total": checklist_total,
                "checklist_completed": checklist_completed,
                "checklist_submitted": checklist_submitted,
                "checklist_rate": checklist_rate,
                "corrections": corrections,
                "quality_score": quality_score,
                "supply_total": len(staff_supply),
                "supply_reviewed": sum(
                    row.status == "reviewed" for row in staff_supply
                ),
                "supply_cost": round(
                    sum(
                        parse_non_negative_quantity(
                            str(getattr(row, "estimated_cost", 0) or 0)
                        )
                        or 0
                        for row in staff_supply
                    ),
                    2,
                ),
            }
        )

    return result


def parse_non_negative_quantity(value):
    try:
        quantity = float(normalize_digits((value or "").strip()))
    except (TypeError, ValueError):
        return None

    return quantity if quantity >= 0 else None


def allowed_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def get_upload_folder():
    folder = current_app.config.get("UPLOAD_FOLDER")
    if not folder:
        folder = os.path.join(current_app.root_path, "static", "uploads")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_uploaded_image(file):
    if not file or not file.filename:
        return None

    if not allowed_file(file.filename):
        return None

    extension = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{extension}"
    filename = secure_filename(filename)

    file.save(os.path.join(get_upload_folder(), filename))
    return filename


def delete_uploaded_image(filename):
    if not filename:
        return

    path = os.path.join(get_upload_folder(), filename)

    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def copy_uploaded_image(filename):
    if not filename:
        return None

    source = os.path.join(get_upload_folder(), filename)
    if not os.path.isfile(source):
        return None

    extension = filename.rsplit(".", 1)[-1].lower()
    copied_name = secure_filename(f"{uuid.uuid4().hex}.{extension}")
    destination = os.path.join(get_upload_folder(), copied_name)

    try:
        with open(source, "rb") as source_file, open(destination, "wb") as destination_file:
            destination_file.write(source_file.read())
    except OSError:
        return None

    return copied_name


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        user = get_current_user()
        if not user:
            session.clear()
            flash("لطفاً ابتدا وارد سامانه شوید.", "warning")
            return redirect(url_for("main.login"))

        if user.role == "manager" and not user.is_appointed_manager:
            allowed_endpoints = {"main.logout", "main.preferences"}
            if active_observer_exists():
                allowed_endpoints.add("main.manager_assignment_pending")
            else:
                allowed_endpoints.update({"main.users", "main.add_user"})
            if request.endpoint not in allowed_endpoints:
                flash(
                    "دسترسی مسئول خدمات تا زمان انتصاب رسمی توسط ناظر اداره‌کل محدود است.",
                    "warning",
                )
                return dashboard_redirect(user)

        return view(*args, **kwargs)

    return wrapped_view


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            user = get_current_user()

            if not user:
                session.clear()
                flash("لطفاً ابتدا وارد سامانه شوید.", "warning")
                return redirect(url_for("main.login"))

            allowed = user.role in roles
            if user.role == "manager" and not user.is_appointed_manager:
                allowed = manager_bootstrap_allowed(user) and "manager" in roles

            if not allowed:
                flash("شما اجازه دسترسی به این صفحه را ندارید.", "danger")
                return dashboard_redirect(user)

            return view(*args, **kwargs)

        return wrapped_view

    return decorator


@main.route("/")
def index():
    user = get_current_user()

    if user:
        return dashboard_redirect(user)

    return redirect(url_for("main.login"))


@main.route("/today-board")
@login_required
def today_board():
    user = get_current_user()

    return render_template(
        "today_board.html",
        today_board=checklist_board_for(user),
        today_board_page=True,
        current_user=user,
    )


@main.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        storage = shutil.disk_usage(current_app.instance_path)
        latest = latest_backup(current_app)
    except Exception:
        current_app.logger.exception("Health check database failure")
        return {
            "service": "pakyar",
            "status": "degraded",
            "database": "unavailable",
        }, 503

    return {
        "service": "pakyar",
        "status": "ok",
        "database": "ok",
        "backups": len(list_backups(current_app)),
        "latest_backup": latest["name"] if latest else None,
        "free_storage_gb": round(storage.free / (1024 ** 3), 2),
    }, 200


@main.route("/sw.js")
def service_worker():
    response = send_from_directory(
        Path(current_app.root_path) / "static",
        "sw.js",
        mimetype="text/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@main.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user():
        return dashboard_redirect()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("نام کاربری و رمز عبور را وارد کنید.", "danger")
            return render_template("login.html")

        user = User.query.filter_by(username=username).first()

        if user and not getattr(user, "is_active", True):
            flash("این حساب کاربری غیرفعال شده است.", "danger")
            return render_template("login.html")

        now = utc_now()
        if (
            user
            and user.locked_until
            and user.locked_until > now
        ):
            remaining_minutes = max(
                int((user.locked_until - now).total_seconds() // 60) + 1,
                1,
            )
            flash(
                f"این حساب موقتاً قفل شده است. حدود {remaining_minutes} دقیقه دیگر تلاش کنید.",
                "warning",
            )
            return render_template("login.html")

        if not verify_password(user, password):
            if user:
                user.failed_login_count = (
                    int(user.failed_login_count or 0) + 1
                )
                if user.failed_login_count >= LOGIN_MAX_ATTEMPTS:
                    user.locked_until = (
                        now + timedelta(minutes=LOGIN_LOCK_MINUTES)
                    )
                    user.failed_login_count = 0
                commit_changes()
            flash("نام کاربری یا رمز عبور نادرست است.", "danger")
            return render_template("login.html")

        if not password_is_hashed(user.password):
            user.password = generate_password_hash(password)
            user.password_changed_at = now

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        if not commit_changes():
            flash("ورود انجام نشد. دوباره تلاش کنید.", "danger")
            return render_template("login.html")

        establish_authenticated_session(user)

        log_activity("ورود به سامانه", f"کاربر {user.username} وارد شد.", user.id)

        return dashboard_redirect(user)

    return render_template("login.html")


@main.route("/logout")
@login_required
def logout():
    user = get_current_user()

    if user:
        log_activity("خروج از سامانه", f"کاربر {user.username} خارج شد.", user.id)

    session.clear()
    flash("با موفقیت از سامانه خارج شدید.", "success")
    return redirect(url_for("main.login"))


@main.route("/manager")
@role_required("manager")
def manager():
    user = get_current_user()
    reports = Report.query.order_by(Report.created_at.desc()).all()
    users = User.query.order_by(User.created_at.desc()).all()
    sections = Section.query.order_by(Section.name.asc()).all()
    selected_month, _month_start, _month_end, selected_month_label = selected_month_range(
        request.args.get("jalali_month") or request.args.get("month", "")
    )
    selected_section_id = request.args.get("section_id", type=int)
    if selected_section_id not in {section.id for section in sections}:
        selected_section_id = None
    selected_shift = request.args.get("shift", "").strip()
    if selected_shift not in CHECKLIST_SHIFTS:
        selected_shift = None
    sent_messages = visible_messages_for(user)
    today_board = checklist_board_for(user)
    workboard = followup_board_for(user, today_board=today_board)
    notifications = visible_notifications_for(user, limit=8)
    dashboard = manager_dashboard_data(
        user,
        reports,
        today_board,
        month_value=selected_month,
        section_id=selected_section_id,
        shift=selected_shift,
    )
    inventory_forecasts = inventory_forecast_for(user)
    status_stats = report_stats(reports)
    stats = {
        "total_users": len(users),
        "total_sections": len(sections),
        "total_reports": len(reports),
        "pending_reports": status_stats["pending"],
        "approved_reports": status_stats["approved"],
        "rejected_reports": status_stats["rejected"],
        "resubmitted_reports": status_stats["resubmitted"],
        "resubmitted_approved_reports": status_stats["resubmitted_approved"],
    }
    message_stats = {
        "new": sum(message.status == "new" for message in sent_messages),
        "in_progress": sum(
            message.status == "in_progress" for message in sent_messages
        ),
        "resolved": sum(message.status == "resolved" for message in sent_messages),
    }

    return render_template(
        "manager.html",
        reports=reports,
        recent_reports=reports[:10],
        users=users,
        sections=sections,
        stats=stats,
        sent_messages=sent_messages[:8],
        message_stats=message_stats,
        today_board=today_board,
        workboard=workboard,
        notifications=notifications,
        dashboard=dashboard,
        dashboard_filters={
            "month": selected_month,
            "month_label": selected_month_label,
            "section_id": selected_section_id,
            "shift": selected_shift or "",
        },
        dashboard_sections=sections,
        dashboard_shifts=CHECKLIST_SHIFTS,
        inventory_forecasts=inventory_forecasts,
        current_user=user,
    )


@main.route("/supervisor")
@role_required("manager", "supervisor")
def supervisor():
    user = get_current_user()
    reports = visible_reports_for(user)
    supply_usages = visible_supply_usages_for(user)
    received_messages = visible_messages_for(user)
    today_board = checklist_board_for(user)
    inventory_alerts = inventory_alerts_for(user)
    inventory_forecasts = inventory_forecast_for(user)
    handoffs = visible_shift_handoffs_for(user)
    workboard = followup_board_for(
        user,
        today_board=today_board,
        inventory_alerts=inventory_alerts,
    )
    notifications = visible_notifications_for(user, limit=8)

    return render_template(
        "supervisor.html",
        reports=reports,
        stats=report_stats(reports),
        supply_usages=supply_usages,
        received_messages=received_messages[:8],
        today_board=today_board,
        inventory_alerts=inventory_alerts,
        inventory_forecasts=inventory_forecasts,
        handoffs=handoffs,
        workboard=workboard,
        notifications=notifications,
        current_user=user,
    )


@main.route("/workboard")
@role_required("manager", "supervisor", "observer")
def workboard():
    user = get_current_user()
    board = followup_board_for(user)

    return render_template(
        "workboard.html",
        workboard=board,
        current_user=user,
    )


@main.route("/performance")
@role_required("manager", "supervisor", "observer")
def performance():
    user = get_current_user()
    performance_data = staff_performance_data_for(user)
    if user.role in {"manager", "observer"}:
        scope_label = "همهٔ پرسنل"
    else:
        scope_label = "پرسنل بخش‌های تحت نظارت"

    return render_template(
        "performance.html",
        performance_data=performance_data,
        scope_label=scope_label,
        current_user=user,
    )


@main.route("/observer")
@role_required("observer")
def observer():
    user = get_current_user()
    reports = visible_reports_for(user)
    sections = Section.query.order_by(Section.name.asc()).all()
    selected_month, _month_start, month_end, _month_label = selected_month_range(
        request.args.get("jalali_month") or request.args.get("month", "")
    )
    selected_section_id = request.args.get("section_id", type=int)
    if selected_section_id not in {section.id for section in sections}:
        selected_section_id = None
    selected_shift = request.args.get("shift", "").strip()
    if selected_shift not in CHECKLIST_SHIFTS:
        selected_shift = None

    month_reports = [
        report for report in reports
        if report.created_at and _month_start <= report.created_at < month_end
    ]
    filtered_reports = [
        report for report in month_reports
        if (not selected_section_id or report.section_id == selected_section_id)
        and (not selected_shift or report.shift == selected_shift)
    ]
    today_board = checklist_board_for(user)
    dashboard = manager_dashboard_data(
        user,
        reports,
        today_board,
        month_value=selected_month,
        section_id=selected_section_id,
        shift=selected_shift,
    )
    appointed_manager = User.query.filter_by(
        role="manager",
        is_appointed_manager=True,
        is_active=True,
    ).first()
    manager_candidates = (
        User.query
        .filter(User.is_active.is_(True), User.role != "observer")
        .order_by(User.full_name.asc())
        .all()
    )

    return render_template(
        "observer.html",
        reports=reports[:20],
        sections=sections,
        selected_month=selected_month,
        selected_section_id=selected_section_id,
        selected_shift=selected_shift or "",
        dashboard=dashboard,
        performance_data=staff_performance_data_for(user, filtered_reports),
        appointed_manager=appointed_manager,
        manager_candidates=manager_candidates,
        current_user=user,
    )


@main.route("/observer/appoint-manager", methods=["POST"])
@role_required("observer")
def appoint_manager():
    user = get_current_user()
    target_id = request.form.get("user_id", type=int)
    target = db.session.get(User, target_id) if target_id else None

    if not target or not target.is_active or target.role == "observer":
        flash("یک حساب فعال و معتبر برای مسئول خدمات انتخاب کنید.", "danger")
        return redirect(url_for("main.observer"))

    former_managers = User.query.filter_by(role="manager").all()
    former_names = [
        previous.full_name
        for previous in former_managers
        if previous.id != target.id
    ]
    for previous in former_managers:
        previous.is_appointed_manager = False

    for previously_appointed in User.query.filter_by(
        is_appointed_manager=True
    ).all():
        previously_appointed.is_appointed_manager = False
    db.session.flush()

    for previous in former_managers:
        if previous.id != target.id:
            # حساب و سوابق حفظ می‌شود؛ فقط دسترسی مدیریتی قبلی برداشته می‌شود.
            previous.role = "staff"
            previous.sections = []

    target.role = "manager"
    target.is_appointed_manager = True

    if not commit_changes():
        flash("انتصاب مسئول خدمات ذخیره نشد. دوباره تلاش کنید.", "danger")
        return redirect(url_for("main.observer"))

    detail = f"{target.full_name} به‌عنوان مسئول خدمات منصوب شد."
    if former_names:
        detail += " دسترسی مدیریتی مسئول قبلی به نقش پرسنل تغییر کرد."
    log_activity("انتصاب مسئول خدمات", detail, user.id)
    create_notifications(
        [target.id],
        "انتصاب مسئول خدمات",
        "ناظر اداره‌کل شما را به‌عنوان مسئول خدمات سامانه منصوب کرد.",
        notification_type="system",
        target_url=url_for("main.manager"),
    )
    flash(f"{target.full_name} به‌عنوان مسئول خدمات منصوب شد.", "success")
    return redirect(url_for("main.observer"))


@main.route("/manager-assignment-pending")
@login_required
def manager_assignment_pending():
    user = get_current_user()
    if user.role != "manager" or user.is_appointed_manager:
        return dashboard_redirect(user)
    return render_template("manager_assignment_pending.html", current_user=user)


@main.route("/reminders")
@login_required
def reminders():
    user = get_current_user()
    rules = visible_reminder_rules_for(user)
    if user.role == "staff":
        events = ReminderEvent.query.filter_by(assigned_user_id=user.id).order_by(ReminderEvent.due_at.desc()).limit(100).all()
    else:
        events = ReminderEvent.query.order_by(ReminderEvent.due_at.desc()).limit(100).all()
    can_manage = user.role in {"manager", "supervisor"}
    sections = Section.query.order_by(Section.name.asc()).all() if user.role in ELEVATED_ROLES else sorted(user.sections, key=lambda item: item.name)
    staff_users = User.query.filter_by(role="staff", is_active=True).order_by(User.full_name.asc()).all()
    return render_template("reminders.html", rules=rules, events=events, can_manage=can_manage, sections=sections, staff_users=staff_users, current_user=user)


@main.route("/reminders/add", methods=["POST"])
@role_required("manager", "supervisor")
def add_reminder():
    user = get_current_user()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    section_id = request.form.get("section_id", type=int)
    try:
        interval = int(request.form.get("interval_minutes", "120"))
    except ValueError:
        interval = 0
    section = db.session.get(Section, section_id) if section_id else None
    allowed_sections = {item.id for item in user.sections} if user.role == "supervisor" else None
    user_ids = {int(value) for value in request.form.getlist("user_ids") if value.isdigit()}
    if not title or not section or interval < 5 or interval > 10080 or (allowed_sections is not None and section.id not in allowed_sections):
        flash("عنوان، بخش معتبر و فاصلهٔ زمانی بین ۵ دقیقه تا ۷ روز لازم است.", "danger")
        return redirect(url_for("main.reminders"))
    valid_staff = {item.id for item in User.query.filter(User.id.in_(user_ids), User.role == "staff", User.is_active.is_(True)).all()} if user_ids else set()
    assigned = valid_staff or {item.id for item in section.assigned_users if item.role == "staff" and item.is_active}
    weekdays = ",".join(request.form.getlist("weekdays")) or "all"
    rule = ReminderRule(
        created_by=user.id, section_id=section.id, title=title, description=description,
        interval_minutes=interval, start_time=request.form.get("start_time") or None,
        end_time=request.form.get("end_time") or None, weekdays=weekdays,
        assigned_user_ids=json.dumps(sorted(assigned)), next_due_at=utc_now() + timedelta(minutes=interval),
    )
    db.session.add(rule)
    db.session.commit()
    log_activity("ایجاد یادآور", f"یادآور «{title}» برای بخش {section.name} ثبت شد.")
    flash("یادآور با موفقیت ثبت شد.", "success")
    return redirect(url_for("main.reminders"))


@main.route("/reminders/<int:rule_id>/toggle", methods=["POST"])
@role_required("manager", "supervisor")
def toggle_reminder(rule_id):
    user = get_current_user()
    rule = db.session.get(ReminderRule, rule_id)
    if not rule or (user.role == "supervisor" and rule.section_id not in {item.id for item in user.sections}):
        flash("یادآور موردنظر در دسترس نیست.", "danger")
        return redirect(url_for("main.reminders"))
    rule.is_active = not rule.is_active
    db.session.commit()
    flash("وضعیت یادآور تغییر کرد.", "success")
    return redirect(url_for("main.reminders"))


@main.route("/reminder-events/<int:event_id>/complete", methods=["POST"])
@login_required
def complete_reminder(event_id):
    user = get_current_user()
    event = db.session.get(ReminderEvent, event_id)
    allowed = event and (
        (event.assigned_user_id == user.id and user.role != "observer")
        or (user.role == "manager" and user.is_appointed_manager)
        or (
            user.role == "supervisor"
            and event.section_id in {item.id for item in user.sections}
        )
    )
    if not allowed:
        flash("این یادآوری برای شما قابل ثبت نیست.", "danger")
        return redirect(url_for("main.reminders"))
    event.status = "completed"
    event.completed_at = utc_now()
    event.completed_by = user.id
    event.note = request.form.get("note", "").strip() or None
    db.session.commit()
    flash("انجام یادآوری ثبت شد.", "success")
    return redirect(url_for("main.reminders"))


@main.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    keys = payload.get("keys") or {}
    endpoint = str(payload.get("endpoint", "")).strip()
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return {"ok": False, "message": "اطلاعات اعلان ناقص است."}, 400
    subscription = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if not subscription:
        subscription = PushSubscription(endpoint=endpoint, user_id=user.id, p256dh=keys["p256dh"], auth=keys["auth"])
        db.session.add(subscription)
    else:
        subscription.user_id = user.id
        subscription.p256dh = keys["p256dh"]
        subscription.auth = keys["auth"]
        subscription.is_active = True
    db.session.commit()
    return {"ok": True}


@main.route("/staff")
@role_required("staff")
def staff():
    user = get_current_user()

    reports = (
        Report.query.filter_by(user_id=user.id)
        .order_by(Report.created_at.desc())
        .all()
    )
    supply_usages = (
        SupplyUsage.query
        .filter_by(user_id=user.id)
        .order_by(SupplyUsage.created_at.desc())
        .all()
    )

    sections = Section.query.order_by(Section.name.asc()).all()
    received_messages = visible_messages_for(user)
    today_board = checklist_board_for(user)
    notifications = visible_notifications_for(user, limit=8)

    return render_template(
        "staff.html",
        reports=reports,
        supply_usages=supply_usages,
        sections=sections,
        received_messages=received_messages[:8],
        today_board=today_board,
        notifications=notifications,
        current_user=user,
    )


@main.route("/checklists")
@login_required
def checklists():
    user = get_current_user()
    sections = visible_checklist_sections_for(user)
    requested_section_id = request.args.get("section_id", type=int)
    selected_frequency = request.args.get("frequency", "daily").strip()
    selected_shift = request.args.get("shift", "صبح").strip()
    selected_weekday = request.args.get("weekday", "").strip()

    if selected_frequency not in CHECKLIST_FREQUENCIES:
        selected_frequency = "daily"

    if selected_frequency == "weekly":
        if selected_shift not in CHECKLIST_SHIFTS:
            selected_shift = "صبح"
        if selected_weekday not in CHECKLIST_WEEKDAY_CODES:
            selected_weekday = current_checklist_weekday()
    else:
        if selected_shift not in CHECKLIST_SHIFTS:
            selected_shift = "صبح"
        selected_weekday = None

    selected_section = (
        db.session.get(Section, requested_section_id)
        if requested_section_id
        else None
    )
    allowed_section_ids = {section.id for section in sections}

    if not selected_section or selected_section.id not in allowed_section_ids:
        selected_section = sections[0] if sections else None

    checklist_date = iran_now().date()
    period_key = checklist_period_key(
        selected_frequency,
        checklist_date,
    )
    items = []
    completion_map = {}
    observations = []
    item_observations = []
    checklist_stats = {
        "items": 0,
        "staff": 0,
        "completed_checks": 0,
        "total_checks": 0,
        "rate": 0,
        "corrections": 0,
    }
    review_completions = []
    handoffs = []

    if selected_section:
        if user.role == "staff":
            items = checklist_items_for(
                selected_section.id,
                selected_frequency,
                selected_shift,
                selected_weekday,
            )
            completion_map = checklist_completion_map(
                items,
                user.id,
                checklist_date,
                period_key,
                selected_shift,
            )
            completed_count = sum(
                checklist_completion_is_done(completion_map.get(item.id))
                for item in items
            )
            correction_count = sum(
                bool(
                    completion_map.get(item.id)
                    and completion_map[item.id].status == "needs_correction"
                )
                for item in items
            )
            checklist_stats = {
                "items": len(items),
                "staff": 1,
                "completed_checks": completed_count,
                "total_checks": len(items),
                "rate": percentage(completed_count, len(items)),
                "corrections": correction_count,
            }
        else:
            (
                items,
                _staff_users,
                observations,
                item_observations,
                checklist_stats,
            ) = checklist_observation(
                selected_section,
                selected_frequency,
                selected_shift,
                selected_weekday,
                checklist_date,
                period_key,
            )
        review_completions = checklist_review_completions(
            user,
            selected_section.id,
            selected_frequency,
            selected_shift,
            selected_weekday,
        )
        handoffs = visible_shift_handoffs_for(
            user,
            selected_section.id,
        )

    return render_template(
        "checklists.html",
        sections=sections,
        selected_section=selected_section,
        selected_frequency=selected_frequency,
        selected_frequency_label=CHECKLIST_FREQUENCY_LABELS[selected_frequency],
        selected_shift=selected_shift,
        selected_weekday=selected_weekday,
        selected_weekday_label=CHECKLIST_WEEKDAY_LABELS.get(
            selected_weekday,
            "-",
        ),
        checklist_shifts=CHECKLIST_SHIFTS,
        checklist_weekdays=CHECKLIST_WEEKDAYS,
        checklist_frequencies=CHECKLIST_FREQUENCIES,
        checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
        checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
        checklist_period_label=checklist_period_label(
            selected_frequency,
            selected_weekday,
            selected_shift,
        ),
        today_weekday=current_checklist_weekday(checklist_date),
        checklist_date_label=format_date_for_user(
            user,
            utc_now(),
            "%Y/%m/%d",
        ),
        items=items,
        completion_map=completion_map,
        observations=observations,
        item_observations=item_observations,
        checklist_stats=checklist_stats,
        review_completions=review_completions,
        handoffs=handoffs,
        checklist_completion_status_label=checklist_completion_status_label,
        current_user=user,
    )


@main.route("/checklists/items/add", methods=["POST"])
@role_required("supervisor")
def add_checklist_item():
    user = get_current_user()
    section_id = request.form.get("section_id", type=int)
    frequency = request.form.get("frequency", "daily").strip()
    shift = request.form.get("shift", "").strip()
    weekday = request.form.get("weekday", "").strip()
    items_text = request.form.get("items_text", "").strip()
    legacy_title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    sort_order = request.form.get("sort_order", type=int)
    section_ids = {section.id for section in user.sections}
    section = db.session.get(Section, section_id)

    if not section or section.id not in section_ids:
        flash("شما به این بخش دسترسی ندارید.", "danger")
        return redirect(url_for("main.checklists"))

    if frequency not in CHECKLIST_FREQUENCIES:
        flash("نوع چک‌لیست معتبر نیست.", "danger")
        return redirect(checklist_redirect_url(section.id))

    if shift not in CHECKLIST_SHIFTS:
        flash("شیفت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(
            checklist_redirect_url(
                section.id,
                frequency,
                "صبح",
                current_checklist_weekday() if frequency == "weekly" else None,
            )
        )
    if frequency == "daily":
        weekday = None
    elif weekday not in CHECKLIST_WEEKDAY_CODES:
        flash("روز هفتهٔ انتخاب‌شده معتبر نیست.", "danger")
        return redirect(
            checklist_redirect_url(
                section.id,
                frequency,
                shift,
                current_checklist_weekday(),
            )
        )

    titles = [
        line.strip()
        for line in (items_text or legacy_title).splitlines()
        if line.strip()
    ]
    if not titles:
        flash("حداقل یک مورد برای چک‌لیست وارد کنید.", "danger")
        return redirect(
            checklist_redirect_url(section.id, frequency, shift, weekday)
        )

    existing_titles = {
        item.title
        for item in (
            ChecklistItem.query
            .filter_by(
                section_id=section.id,
                frequency=frequency,
                shift=shift,
                weekday=weekday,
                is_active=True,
            )
            .all()
        )
    }
    unique_titles = []
    seen_titles = set()
    for title in titles:
        if title not in existing_titles and title not in seen_titles:
            unique_titles.append(title)
            seen_titles.add(title)

    if not unique_titles:
        flash("همهٔ موارد واردشده قبلاً در همین چک‌لیست وجود دارند.", "warning")
        return redirect(
            checklist_redirect_url(section.id, frequency, shift, weekday)
        )

    first_order = max(sort_order or 0, 0)
    for offset, title in enumerate(unique_titles):
        db.session.add(
            ChecklistItem(
                section_id=section.id,
                created_by=user.id,
                shift=shift,
                frequency=frequency,
                weekday=weekday,
                title=title,
                description=description or None,
                sort_order=first_order + offset,
                is_active=True,
            )
        )

    if commit_changes():
        log_activity(
            "تعریف مورد چک‌لیست",
            f"{len(unique_titles)} مورد برای {CHECKLIST_FREQUENCY_LABELS[frequency]} "
            f"بخش {section.name} تعریف شد.",
        )
        flash(
            f"{len(unique_titles)} مورد چک‌لیست با موفقیت اضافه شد.",
            "success",
        )
    else:
        flash("افزودن مورد چک‌لیست انجام نشد.", "danger")

    return redirect(
        checklist_redirect_url(section.id, frequency, shift, weekday)
    )


@main.route("/checklists/items/<int:item_id>/edit", methods=["GET", "POST"])
@role_required("supervisor")
def edit_checklist_item(item_id):
    user = get_current_user()
    item = db.session.get(ChecklistItem, item_id)
    section_ids = {section.id for section in user.sections}

    if not item or item.section_id not in section_ids or not item.is_active:
        flash("مورد چک‌لیست پیدا نشد یا به شما مربوط نیست.", "danger")
        return redirect(url_for("main.checklists"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        frequency = request.form.get("frequency", item.frequency).strip()
        shift = request.form.get("shift", "").strip()
        weekday = request.form.get("weekday", "").strip()
        sort_order = request.form.get("sort_order", type=int)

        if not title:
            flash("عنوان مورد چک‌لیست را وارد کنید.", "danger")
            return render_template(
                "checklist_item_edit.html",
                item=item,
                checklist_shifts=CHECKLIST_SHIFTS,
                checklist_weekdays=CHECKLIST_WEEKDAYS,
                checklist_frequencies=CHECKLIST_FREQUENCIES,
                checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
                checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
                current_user=user,
            )

        if frequency not in CHECKLIST_FREQUENCIES:
            flash("نوع چک‌لیست معتبر نیست.", "danger")
            return render_template(
                "checklist_item_edit.html",
                item=item,
                checklist_shifts=CHECKLIST_SHIFTS,
                checklist_weekdays=CHECKLIST_WEEKDAYS,
                checklist_frequencies=CHECKLIST_FREQUENCIES,
                checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
                checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
                current_user=user,
            )

        if shift not in CHECKLIST_SHIFTS:
            flash("شیفت انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "checklist_item_edit.html",
                item=item,
                checklist_shifts=CHECKLIST_SHIFTS,
                checklist_weekdays=CHECKLIST_WEEKDAYS,
                checklist_frequencies=CHECKLIST_FREQUENCIES,
                checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
                checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
                current_user=user,
            )
        if frequency == "daily":
            weekday = None
        elif weekday not in CHECKLIST_WEEKDAY_CODES:
            flash("روز هفتهٔ انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "checklist_item_edit.html",
                item=item,
                checklist_shifts=CHECKLIST_SHIFTS,
                checklist_weekdays=CHECKLIST_WEEKDAYS,
                checklist_frequencies=CHECKLIST_FREQUENCIES,
                checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
                checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
                current_user=user,
            )

        duplicate = (
            ChecklistItem.query
            .filter(
                ChecklistItem.id != item.id,
                ChecklistItem.section_id == item.section_id,
                ChecklistItem.frequency == frequency,
                ChecklistItem.shift == shift,
                ChecklistItem.weekday == weekday,
                ChecklistItem.title == title,
                ChecklistItem.is_active.is_(True),
            )
            .first()
        )
        if duplicate:
            flash("مورد مشابهی برای این بخش و شیفت وجود دارد.", "warning")
            return render_template(
                "checklist_item_edit.html",
                item=item,
                checklist_shifts=CHECKLIST_SHIFTS,
                checklist_weekdays=CHECKLIST_WEEKDAYS,
                checklist_frequencies=CHECKLIST_FREQUENCIES,
                checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
                checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
                current_user=user,
            )

        item.title = title
        item.description = description or None
        item.frequency = frequency
        item.shift = shift
        item.weekday = weekday
        item.sort_order = max(sort_order or 0, 0)

        if commit_changes():
            log_activity(
                "ویرایش مورد چک‌لیست",
                f"مورد چک‌لیست شماره {item.id} ویرایش شد.",
            )
            flash("مورد چک‌لیست به‌روزرسانی شد.", "success")
            return redirect(
                checklist_redirect_url(
                    item.section_id,
                    item.frequency,
                    item.shift,
                    item.weekday,
                )
            )

        flash("ویرایش مورد چک‌لیست انجام نشد.", "danger")

    return render_template(
        "checklist_item_edit.html",
        item=item,
        checklist_shifts=CHECKLIST_SHIFTS,
        checklist_weekdays=CHECKLIST_WEEKDAYS,
        checklist_frequencies=CHECKLIST_FREQUENCIES,
        checklist_frequency_labels=CHECKLIST_FREQUENCY_LABELS,
        checklist_weekday_labels=CHECKLIST_WEEKDAY_LABELS,
        current_user=user,
    )


@main.route("/checklists/items/<int:item_id>/delete", methods=["POST"])
@role_required("supervisor")
def delete_checklist_item(item_id):
    user = get_current_user()
    item = db.session.get(ChecklistItem, item_id)
    section_ids = {section.id for section in user.sections}

    if not item or item.section_id not in section_ids:
        flash("مورد چک‌لیست پیدا نشد یا به شما مربوط نیست.", "danger")
        return redirect(url_for("main.checklists"))

    section_id = item.section_id
    frequency = item.frequency
    shift = item.shift
    weekday = item.weekday
    item.is_active = False

    if commit_changes():
        log_activity(
            "حذف مورد چک‌لیست",
            f"مورد چک‌لیست شماره {item.id} غیرفعال شد.",
        )
        flash("مورد چک‌لیست حذف شد.", "success")
    else:
        flash("حذف مورد چک‌لیست انجام نشد.", "danger")

    return redirect(
        checklist_redirect_url(section_id, frequency, shift, weekday)
    )


@main.route("/checklists/save", methods=["POST"])
@role_required("staff")
def save_checklist():
    user = get_current_user()
    section_id = request.form.get("section_id", type=int)
    frequency = request.form.get("frequency", "daily").strip()
    shift = request.form.get("shift", "").strip()
    weekday = request.form.get("weekday", "").strip()
    section_ids = {section.id for section in user.sections}
    section = db.session.get(Section, section_id)

    if not section or section.id not in section_ids:
        flash("بخش انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.checklists"))

    if frequency not in CHECKLIST_FREQUENCIES:
        flash("نوع چک‌لیست معتبر نیست.", "danger")
        return redirect(checklist_redirect_url(section.id))

    if shift not in CHECKLIST_SHIFTS:
        flash("شیفت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(
            checklist_redirect_url(
                section.id,
                frequency,
                "صبح",
                current_checklist_weekday() if frequency == "weekly" else None,
            )
        )
    if frequency == "daily":
        weekday = None
    elif weekday not in CHECKLIST_WEEKDAY_CODES:
        flash("روز هفتهٔ انتخاب‌شده معتبر نیست.", "danger")
        return redirect(
            checklist_redirect_url(
                section.id,
                frequency,
                shift,
                current_checklist_weekday(),
            )
        )

    items = checklist_items_for(section.id, frequency, shift, weekday)
    item_ids = {item.id for item in items}
    completed_ids = set()
    for raw_id in request.form.getlist("completed_item_ids"):
        try:
            completed_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    completed_ids &= item_ids
    checklist_note = request.form.get("checklist_note", "").strip()
    evidence_image = request.files.get("evidence_image")
    evidence_file = None
    if evidence_image and evidence_image.filename:
        evidence_file = save_uploaded_image(evidence_image)
        if not evidence_file:
            flash("فرمت تصویر چک‌لیست مجاز نیست.", "danger")
            return redirect(
                checklist_redirect_url(section.id, frequency, shift, weekday)
            )

    checklist_date = iran_now().date()
    period_key = checklist_period_key(frequency, checklist_date)
    existing = {}

    if item_ids:
        existing = {
            completion.item_id: completion
            for completion in (
                ChecklistCompletion.query
                .filter(
                    ChecklistCompletion.item_id.in_(item_ids),
                    ChecklistCompletion.user_id == user.id,
                    ChecklistCompletion.period_key == period_key,
                    ChecklistCompletion.shift == shift,
                )
                .all()
            )
        }

    now = utc_now()
    for item in items:
        completion = existing.get(item.id)
        if not completion:
            completion = ChecklistCompletion(
                item_id=item.id,
                user_id=user.id,
                checklist_date=checklist_date,
                period_key=period_key,
                shift=shift,
            )
            db.session.add(completion)

        completion.shift = shift
        completion.is_completed = item.id in completed_ids
        completion.completed_at = now if item.id in completed_ids else None
        completion.status = (
            "completed" if item.id in completed_ids else "pending"
        )
        completion.note = checklist_note or None
        if item.id in completed_ids and evidence_file:
            completion.image_file = evidence_file
        elif item.id not in completed_ids:
            completion.image_file = None
        completion.reviewed_by = None
        completion.reviewed_at = None
        completion.review_note = None

    if commit_changes():
        notify_section_reviewers(
            section.id,
            "ثبت چک‌لیست جدید",
            f"{user.full_name} چک‌لیست {CHECKLIST_FREQUENCY_LABELS[frequency]} "
            f"بخش «{section.name}» را در شیفت {shift} ثبت کرد.",
            notification_type="checklist",
            target_url=checklist_redirect_url(
                section.id,
                frequency,
                shift,
                weekday,
            ),
            exclude_ids={user.id},
        )
        log_activity(
            "ثبت چک‌لیست نظافت",
            f"{CHECKLIST_FREQUENCY_LABELS[frequency]} بخش {section.name} "
            f"در {shift} ثبت شد.",
        )
        flash("وضعیت چک‌لیست با موفقیت ثبت شد.", "success")
    else:
        delete_uploaded_image(evidence_file)
        flash("ثبت وضعیت چک‌لیست انجام نشد.", "danger")

    return redirect(
        checklist_redirect_url(section.id, frequency, shift, weekday)
    )


@main.route("/checklists/completions/<int:completion_id>/review", methods=["POST"])
@role_required("manager", "supervisor")
def review_checklist_completion(completion_id):
    user = get_current_user()
    completion = db.session.get(ChecklistCompletion, completion_id)

    if not completion or not completion.item:
        flash("ثبت چک‌لیست موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.checklists"))

    section_id = completion.item.section_id
    if user.role == "supervisor" and section_id not in {
        section.id for section in user.sections
    }:
        flash("شما اجازه بررسی این چک‌لیست را ندارید.", "danger")
        return redirect(url_for("main.checklists"))

    status = request.form.get("status", "").strip()
    review_note = request.form.get("review_note", "").strip()
    if status not in {"approved", "needs_correction"}:
        flash("وضعیت بررسی چک‌لیست معتبر نیست.", "danger")
        return redirect(url_for("main.checklists"))

    if status == "needs_correction" and not review_note:
        flash("برای درخواست اصلاح، توضیح مسئول بخش را وارد کنید.", "danger")
        return redirect(
            checklist_redirect_url(
                section_id,
                completion.item.frequency,
                completion.shift,
                completion.item.weekday,
            )
        )

    completion.status = status
    completion.is_completed = True
    completion.reviewed_by = user.id
    completion.reviewed_at = utc_now()
    completion.review_note = review_note or None

    if commit_changes():
        title = (
            "چک‌لیست تأیید شد"
            if status == "approved"
            else "اصلاح چک‌لیست لازم است"
        )
        body = (
            f"ثبت چک‌لیست «{completion.item.title}» در بخش "
            f"«{completion.item.section.name}» {checklist_completion_status_label(status)}."
        )
        if review_note:
            body += f" توضیح: {review_note}"
        create_notifications(
            [completion.user_id],
            title,
            body,
            notification_type="checklist",
            target_url=checklist_redirect_url(
                section_id,
                completion.item.frequency,
                completion.shift,
                completion.item.weekday,
            ),
        )
        log_activity(
            "بررسی چک‌لیست",
            f"ثبت چک‌لیست شماره {completion.id} به {status} تغییر کرد.",
        )
        flash("وضعیت چک‌لیست با موفقیت ثبت شد.", "success")
    else:
        flash("بررسی چک‌لیست ذخیره نشد.", "danger")

    return redirect(
        checklist_redirect_url(
            section_id,
            completion.item.frequency,
            completion.shift,
            completion.item.weekday,
        )
    )


@main.route("/checklists/handoff", methods=["POST"])
@role_required("staff")
def save_shift_handoff():
    user = get_current_user()
    section_id = request.form.get("section_id", type=int)
    shift = request.form.get("shift", "").strip()
    note = request.form.get("note", "").strip()
    section = db.session.get(Section, section_id)

    if not section or section not in user.sections:
        flash("بخش انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.checklists"))
    if shift not in CHECKLIST_SHIFTS:
        flash("شیفت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.checklists", section_id=section.id))
    if not note:
        flash("یادداشت تحویل شیفت را وارد کنید.", "danger")
        return redirect(
            url_for(
                "main.checklists",
                section_id=section.id,
                frequency="daily",
                shift=shift,
            )
        )

    handoff_date = iran_now().date()
    handoff = (
        ShiftHandoff.query
        .filter_by(
            section_id=section.id,
            user_id=user.id,
            handoff_date=handoff_date,
            shift=shift,
        )
        .first()
    )
    if not handoff:
        handoff = ShiftHandoff(
            section_id=section.id,
            user_id=user.id,
            handoff_date=handoff_date,
            shift=shift,
        )
        db.session.add(handoff)

    handoff.note = note
    handoff.is_acknowledged = False
    handoff.acknowledged_by = None
    handoff.acknowledged_at = None

    if commit_changes():
        notify_section_reviewers(
            section.id,
            "یادداشت تحویل شیفت جدید",
            f"{user.full_name} برای بخش «{section.name}» یادداشت تحویل شیفت ثبت کرد.",
            notification_type="checklist",
            target_url=url_for("main.supervisor"),
            exclude_ids={user.id},
        )
        log_activity(
            "ثبت تحویل شیفت",
            f"یادداشت تحویل شیفت {shift} بخش {section.name} ثبت شد.",
        )
        flash("یادداشت تحویل شیفت برای مسئول بخش ارسال شد.", "success")
    else:
        flash("ثبت یادداشت تحویل شیفت انجام نشد.", "danger")

    return redirect(
        url_for(
            "main.checklists",
            section_id=section.id,
            frequency="daily",
            shift=shift,
        )
    )


@main.route("/checklists/handoff/<int:handoff_id>/ack", methods=["POST"])
@role_required("manager", "supervisor")
def acknowledge_shift_handoff(handoff_id):
    user = get_current_user()
    handoff = db.session.get(ShiftHandoff, handoff_id)

    if not handoff:
        flash("یادداشت تحویل شیفت پیدا نشد.", "danger")
        return redirect(url_for("main.supervisor"))
    if user.role == "supervisor" and handoff.section_id not in {
        section.id for section in user.sections
    }:
        flash("شما اجازه مشاهده این تحویل شیفت را ندارید.", "danger")
        return redirect(url_for("main.supervisor"))

    handoff.is_acknowledged = True
    handoff.acknowledged_by = user.id
    handoff.acknowledged_at = utc_now()
    if commit_changes():
        create_notifications(
            [handoff.user_id],
            "تحویل شیفت دیده شد",
            f"یادداشت تحویل شیفت بخش «{handoff.section.name}» توسط {user.full_name} دیده شد.",
            notification_type="checklist",
            target_url=url_for("main.staff"),
        )
        flash("تحویل شیفت به‌عنوان دیده‌شده ثبت شد.", "success")
    else:
        flash("ثبت مشاهده تحویل شیفت انجام نشد.", "danger")

    return redirect(url_for("main.supervisor"))


@main.route("/messages")
@login_required
def messages():
    user = get_current_user()
    user_messages = visible_messages_for(user)

    return render_template(
        "messages.html",
        messages=user_messages,
        message_mode=(
            "audit"
            if user.role == "observer"
            else "sent" if user.role == "manager" else "received"
        ),
        message_status_label=message_status_label,
        message_priority_label=message_priority_label,
        current_user=user,
    )


@main.route("/notifications")
@login_required
def notifications_page():
    user = get_current_user()
    user_notifications = visible_notifications_for(user)
    return render_template(
        "notifications.html",
        notifications=user_notifications,
        current_user=user,
    )


@main.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():
    user = get_current_user()

    if request.method == "POST":
        form_action = request.form.get("action", "save_preferences").strip()

        if form_action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not verify_password(user, current_password):
                flash("رمز عبور فعلی صحیح نیست.", "danger")
                return redirect(url_for("main.preferences"))

            if len(new_password) < 8:
                flash("رمز عبور جدید باید حداقل ۸ کاراکتر باشد.", "danger")
                return redirect(url_for("main.preferences"))

            if new_password != confirm_password:
                flash("رمز عبور جدید و تکرار آن یکسان نیستند.", "danger")
                return redirect(url_for("main.preferences"))

            if verify_password(user, new_password):
                flash("رمز عبور جدید باید با رمز فعلی متفاوت باشد.", "warning")
                return redirect(url_for("main.preferences"))

            user.password = generate_password_hash(new_password)
            if commit_changes():
                establish_authenticated_session(user)
                log_activity(
                    "تغییر رمز ورود",
                    "رمز عبور حساب کاربر تغییر کرد.",
                    user.id,
                )
                flash("رمز عبور با موفقیت تغییر کرد.", "success")
            else:
                flash("تغییر رمز عبور انجام نشد. دوباره تلاش کنید.", "danger")

            return redirect(url_for("main.preferences"))

        if form_action == "reset_preferences" or request.form.get("reset") == "1":
            values = dict(DEFAULT_PREFERENCES)
        else:
            values = {
                "theme": request.form.get("theme", "").strip(),
                "font_scale": request.form.get("font_scale", "").strip(),
                "date_mode": request.form.get("date_mode", "").strip(),
                "compact_mode": request.form.get("compact_mode") == "1",
                "reduced_motion": request.form.get("reduced_motion") == "1",
            }

        if values["theme"] not in PREFERENCE_THEMES:
            values["theme"] = DEFAULT_PREFERENCES["theme"]
        if values["font_scale"] not in FONT_SCALE_OPTIONS:
            values["font_scale"] = DEFAULT_PREFERENCES["font_scale"]
        if values["date_mode"] not in DATE_MODE_OPTIONS:
            values["date_mode"] = DEFAULT_PREFERENCES["date_mode"]

        user.theme = values["theme"]
        user.font_scale = values["font_scale"]
        user.date_mode = values["date_mode"]
        user.compact_mode = values["compact_mode"]
        user.reduced_motion = values["reduced_motion"]

        if commit_changes():
            apply_preferences_to_session(user)
            log_activity(
                "تغییر شخصی‌سازی سامانه",
                "تنظیمات ظاهری کاربر به‌روزرسانی شد.",
            )
            flash("تنظیمات ظاهری شما ذخیره شد.", "success")
        else:
            flash("ذخیره تنظیمات انجام نشد. دوباره تلاش کنید.", "danger")

        return redirect(url_for("main.preferences"))

    return render_template(
        "preferences.html",
        current_user=user,
        theme_options=PREFERENCE_THEMES,
        font_scale_options=FONT_SCALE_OPTIONS,
        date_mode_options=DATE_MODE_OPTIONS,
    )


@main.route("/notifications/<int:notification_id>/read")
@login_required
def read_notification(notification_id):
    user = get_current_user()
    notification = db.session.get(Notification, notification_id)

    if not notification or notification.user_id != user.id:
        flash("اعلان موردنظر پیدا نشد.", "warning")
        return redirect(url_for("main.notifications_page"))

    notification.is_read = True
    commit_changes()
    if notification.target_url:
        return redirect(notification.target_url)
    return redirect(url_for("main.notifications_page"))


@main.route("/notifications/read-all", methods=["POST"])
@login_required
def read_all_notifications():
    user = get_current_user()
    unread = visible_notifications_for(user, unread_only=True)
    for notification in unread:
        notification.is_read = True
    if unread:
        commit_changes()
    flash("همهٔ اعلان‌ها به‌عنوان خوانده‌شده ثبت شدند.", "success")
    return redirect(url_for("main.notifications_page"))


@main.route("/messages/add", methods=["GET", "POST"])
@role_required("manager")
def add_message():
    user = get_current_user()
    recipients = (
        User.query
        .filter(User.id != user.id, User.role.in_(("supervisor", "staff")))
        .order_by(User.full_name.asc())
        .all()
    )
    sections = Section.query.order_by(Section.name.asc()).all()

    if request.method == "POST":
        recipient_id = request.form.get("recipient_id", type=int)
        section_id = request.form.get("section_id", type=int) or None
        subject = request.form.get("subject", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "normal").strip()

        recipient = db.session.get(User, recipient_id)
        section = db.session.get(Section, section_id) if section_id else None

        if not recipient or recipient.role not in {"supervisor", "staff"}:
            flash("گیرنده پیام را به‌درستی انتخاب کنید.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        if section_id and not section:
            flash("بخش انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        if section and section not in recipient.sections:
            flash("بخش انتخاب‌شده به این گیرنده اختصاص داده نشده است.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        if not subject or not description:
            flash("موضوع و شرح مشکل را کامل کنید.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        if priority not in MESSAGE_PRIORITIES:
            flash("اولویت انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        message = ManagerMessage(
            sender_id=user.id,
            recipient_id=recipient.id,
            section_id=section.id if section else None,
            subject=subject,
            description=description,
            priority=priority,
            status="new",
            is_seen=False,
        )
        db.session.add(message)

        if not commit_changes():
            flash("ارسال پیام انجام نشد.", "danger")
            return render_template(
                "message_form.html",
                recipients=recipients,
                sections=sections,
                current_user=user,
            )

        sender_role_label = role_label(user.role)
        create_notifications(
            [recipient.id],
            f"اطلاع جدید از {sender_role_label}",
            f"پیام «{subject}» برای شما ثبت شده است.",
            notification_type="message",
            target_url=url_for(
                "main.message_detail",
                message_id=message.id,
            ),
        )
        log_activity(
            "ارسال پیام پیگیری",
            f"پیام «{subject}» برای {recipient.full_name} ارسال شد.",
        )
        flash("اطلاع مشکل با موفقیت برای گیرنده ارسال شد.", "success")
        return redirect(url_for("main.messages"))

    return render_template(
        "message_form.html",
        recipients=recipients,
        sections=sections,
        current_user=user,
    )


@main.route("/messages/<int:message_id>")
@login_required
def message_detail(message_id):
    user = get_current_user()
    message = db.session.get(ManagerMessage, message_id)

    if not message:
        flash("پیام موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.messages"))

    can_view = (
        True
        if user.role == "observer"
        else message.sender_id == user.id
        if user.role == "manager"
        else message.recipient_id == user.id
    )
    if not can_view:
        flash("شما اجازه مشاهده این پیام را ندارید.", "danger")
        return redirect(url_for("main.messages"))

    if (
        user.role != "observer"
        and message.recipient_id == user.id
        and not message.is_seen
    ):
        message.is_seen = True
        commit_changes()

    return render_template(
        "message_detail.html",
        message=message,
        message_status_label=message_status_label,
        message_priority_label=message_priority_label,
        current_user=user,
    )


@main.route("/messages/<int:message_id>/respond", methods=["POST"])
@role_required("supervisor", "staff")
def respond_message(message_id):
    user = get_current_user()
    message = db.session.get(ManagerMessage, message_id)

    if not message or message.recipient_id != user.id:
        flash("پیام موردنظر پیدا نشد یا به شما تعلق ندارد.", "danger")
        return redirect(url_for("main.messages"))

    response = request.form.get("response", "").strip()
    status = request.form.get("status", "in_progress").strip()

    if not response:
        flash("متن پاسخ را وارد کنید.", "danger")
        return redirect(url_for("main.message_detail", message_id=message.id))

    if status not in {"in_progress", "resolved"}:
        flash("وضعیت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.message_detail", message_id=message.id))

    message.response = response
    message.status = status
    message.responded_at = utc_now()
    message.is_seen = True

    if commit_changes():
        create_notifications(
            [message.sender_id],
            "پاسخ جدید به پیام",
            f"{user.full_name} به پیام «{message.subject}» پاسخ داد.",
            notification_type="message",
            target_url=url_for(
                "main.message_detail",
                message_id=message.id,
            ),
        )
        log_activity(
            "پاسخ به پیام پیگیری",
            f"پاسخ پیام شماره {message.id} توسط {user.full_name} ثبت شد.",
        )
        flash("پاسخ شما با موفقیت برای مسئول خدمات ارسال شد.", "success")
    else:
        flash("ثبت پاسخ انجام نشد.", "danger")

    return redirect(url_for("main.message_detail", message_id=message.id))


@main.route("/users")
@role_required("manager")
def users():
    current_user = get_current_user()
    all_users = User.query.order_by(User.created_at.desc()).all()
    sections = Section.query.order_by(Section.name.asc()).all()

    return render_template(
        "users.html",
        users=all_users,
        sections=sections,
        bootstrap_mode=manager_bootstrap_allowed(current_user),
        current_user=current_user,
    )


@main.route("/users/add", methods=["GET", "POST"])
@role_required("manager")
def add_user():
    sections = Section.query.order_by(Section.name.asc()).all()
    current_user = get_current_user()
    bootstrap_mode = manager_bootstrap_allowed(current_user)
    role_options = (
        [("observer", "ناظر اداره‌کل")]
        if bootstrap_mode
        else [("staff", "پرسنل"), ("supervisor", "مسئول بخش"), ("oh_expert", "کارشناس بهداشت حرفه‌ای")]
    )

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff").strip()
        section_ids = request.form.getlist("section_ids")

        if not username or not password:
            flash("نام کاربری و رمز عبور الزامی است.", "danger")
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )
        if len(password) < 8:
            flash("رمز عبور باید حداقل ۸ کاراکتر باشد.", "danger")
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )

        if role not in ALLOWED_ROLES:
            flash("نقش انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )

        allowed_roles = {value for value, _label in role_options}
        if role not in allowed_roles:
            flash(
                "این نقش از این بخش قابل واگذاری نیست.",
                "danger",
            )
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )

        if User.query.filter_by(username=username).first():
            flash("این نام کاربری قبلاً ثبت شده است.", "danger")
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )

        user = User(
            username=username,
            full_name=full_name or "کاربر سامانه",
            password=generate_password_hash(password),
            role=role,
        )

        selected_sections = [
            section
            for section in sections
            if str(section.id) in section_ids
        ] if role != "observer" else []

        user.sections = selected_sections
        db.session.add(user)

        if not commit_changes():
            flash("ثبت کاربر انجام نشد.", "danger")
            return render_template(
                "user_form.html",
                sections=sections,
                current_user=current_user,
                bootstrap_mode=bootstrap_mode,
                role_options=role_options,
            )

        log_activity("ایجاد کاربر", f"کاربر {username} ایجاد شد.")
        flash("کاربر با موفقیت ایجاد شد.", "success")
        if user.role == "staff" and request.form.get("complete_work_profile") == "1":
            return redirect(url_for("workload.profile", user_id=user.id))
        return redirect(url_for("main.users"))

    return render_template(
        "user_form.html",
        sections=sections,
        current_user=current_user,
        bootstrap_mode=bootstrap_mode,
        role_options=role_options,
    )


@main.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@role_required("manager")
def edit_user(user_id):
    user = db.session.get(User, user_id)
    sections = Section.query.order_by(Section.name.asc()).all()
    current_user = get_current_user()

    if not user:
        flash("کاربر موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.users"))

    if user.role == "observer" or (
        user.role == "manager" and user.id != current_user.id
    ):
        flash("حساب‌های ناظر و مسئول خدمات از این بخش قابل ویرایش نیستند.", "danger")
        return redirect(url_for("main.users"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", user.role).strip()
        section_ids = request.form.getlist("section_ids")
        is_active = request.form.get("is_active") == "1"

        if not username:
            flash("نام کاربری الزامی است.", "danger")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
            )

        if role not in ALLOWED_ROLES:
            flash("نقش انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
                current_user=current_user,
            )

        permitted_roles = (
            {"manager"}
            if user.role == "manager" and user.id == current_user.id
            else {"staff", "supervisor", "oh_expert"}
        )
        if role not in permitted_roles:
            flash("فقط ناظر اداره‌کل می‌تواند مسئول خدمات را منصوب کند.", "danger")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
                current_user=current_user,
            )

        if user.id == current_user.id and role != user.role:
            flash("نقش حساب فعلی را نمی‌توان از داخل همان حساب تغییر داد.", "warning")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
                current_user=current_user,
            )

        duplicate = (
            User.query
            .filter(User.username == username, User.id != user.id)
            .first()
        )
        if duplicate:
            flash("این نام کاربری قبلاً استفاده شده است.", "danger")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
            )

        user.username = username
        user.full_name = full_name or "کاربر سامانه"
        user.role = role
        user.is_active = True if current_user.id == user.id else is_active
        if user.role != "manager":
            user.sections = [
                section
                for section in sections
                if str(section.id) in section_ids
            ]

        if password:
            if len(password) < 8:
                flash("رمز عبور جدید باید حداقل ۸ کاراکتر باشد.", "danger")
                return render_template(
                    "user_edit.html",
                    user=user,
                    sections=sections,
                    current_user=current_user,
                )
            user.password = generate_password_hash(password)
            user.password_changed_at = utc_now()
            user.failed_login_count = 0
            user.locked_until = None

        if not commit_changes():
            flash("ویرایش کاربر انجام نشد.", "danger")
            return render_template(
                "user_edit.html",
                user=user,
                sections=sections,
            )

        log_activity("ویرایش کاربر", f"کاربر {username} ویرایش شد.")
        flash("اطلاعات کاربر با موفقیت به‌روزرسانی شد.", "success")
        return redirect(url_for("main.users"))

    return render_template(
        "user_edit.html",
        user=user,
        sections=sections,
        current_user=current_user,
    )


@main.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@role_required("manager")
def toggle_user_active(user_id):
    current_user = get_current_user()
    user = db.session.get(User, user_id)

    if not user:
        flash("کاربر موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.users"))
    if user.id == current_user.id:
        flash("حساب کاربری فعلی را نمی‌توان غیرفعال کرد.", "warning")
        return redirect(url_for("main.users"))
    if user.role == "physician":
        flash("حساب پزشک قدیمی غیرفعال می‌ماند؛ ابتدا در ویرایش کاربر یک نقش موجود به آن بدهید.", "warning")
        return redirect(url_for("main.users"))
    if user.role in ELEVATED_ROLES:
        flash("حساب‌های مدیریتی از این بخش قابل غیرفعال‌سازی نیستند.", "danger")
        return redirect(url_for("main.users"))

    user.is_active = not bool(getattr(user, "is_active", True))
    if user.is_active:
        user.failed_login_count = 0
        user.locked_until = None

    if commit_changes():
        state = "فعال" if user.is_active else "غیرفعال"
        log_activity(
            "تغییر وضعیت کاربر",
            f"حساب {user.username} {state} شد.",
        )
        flash(f"حساب کاربر اکنون {state} است.", "success")
    else:
        flash("تغییر وضعیت حساب انجام نشد.", "danger")

    return redirect(url_for("main.users"))


@main.route("/users/<int:user_id>/delete", methods=["POST"])
@role_required("manager")
def delete_user(user_id):
    user = db.session.get(User, user_id)

    if not user:
        flash("کاربر موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.users"))

    current_user = get_current_user()

    if current_user.id == user.id:
        flash("نمی‌توانید حساب خودتان را حذف کنید.", "danger")
        return redirect(url_for("main.users"))
    if user.role == "physician":
        flash("حساب قدیمی پزشک را حذف نکنید تا سابقه‌های قبلی حفظ شود؛ در صورت نیاز نقش آن را تغییر دهید.", "warning")
        return redirect(url_for("main.users"))
    if user.role in ELEVATED_ROLES:
        flash("حساب‌های مدیریتی از این بخش قابل حذف نیستند.", "danger")
        return redirect(url_for("main.users"))

    username = user.username
    db.session.delete(user)

    if commit_changes():
        log_activity("حذف کاربر", f"کاربر {username} حذف شد.")
        flash("کاربر با موفقیت حذف شد.", "success")
    else:
        flash("حذف کاربر انجام نشد.", "danger")

    return redirect(url_for("main.users"))


@main.route("/sections")
@role_required("manager", "supervisor", "observer")
def sections():
    user = get_current_user()
    if user.role in ELEVATED_ROLES:
        all_sections = Section.query.order_by(Section.name.asc()).all()
    else:
        all_sections = sorted(user.sections, key=lambda section: section.name)

    return render_template(
        "sections.html",
        sections=all_sections,
        current_user=user,
    )


@main.route("/sections/add", methods=["GET", "POST"])
@role_required("manager")
def add_section():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        floor = request.form.get("floor", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("نام بخش را وارد کنید.", "danger")
            return render_template("section_form.html")

        section = Section(
            name=name,
            floor=floor or None,
            description=description or None,
        )

        db.session.add(section)

        if not commit_changes():
            flash("ثبت بخش انجام نشد.", "danger")
            return render_template("section_form.html")

        log_activity("ایجاد بخش", f"بخش {name} ایجاد شد.")
        flash("بخش با موفقیت ایجاد شد.", "success")
        return redirect(url_for("main.sections"))

    return render_template("section_form.html")


@main.route("/sections/<int:section_id>/delete", methods=["POST"])
@role_required("manager")
def delete_section(section_id):
    section = db.session.get(Section, section_id)

    if not section:
        flash("بخش موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.sections"))

    name = section.name
    db.session.delete(section)

    if commit_changes():
        log_activity("حذف بخش", f"بخش {name} حذف شد.")
        flash("بخش با موفقیت حذف شد.", "success")
    else:
        flash("حذف بخش انجام نشد.", "danger")

    return redirect(url_for("main.sections"))


@main.route("/report/add", methods=["GET", "POST"])
@role_required("staff", "supervisor")
def add_report():
    user = get_current_user()

    sections = list(user.sections)

    if request.method == "POST":
        section_id = request.form.get("section_id", type=int)
        shift = request.form.get("shift", "").strip()
        description = request.form.get("description", "").strip()
        image = request.files.get("image")

        section = db.session.get(Section, section_id)

        if not section:
            flash("بخش انتخاب‌شده معتبر نیست.", "danger")
            return render_template("report_form.html", sections=sections)

        if user.role in {"staff", "supervisor"} and section not in user.sections:
            flash("شما به این بخش دسترسی ندارید.", "danger")
            return render_template("report_form.html", sections=sections)

        image_file = None

        if image and image.filename:
            image_file = save_uploaded_image(image)

            if not image_file:
                flash("فرمت تصویر مجاز نیست.", "danger")
                return render_template("report_form.html", sections=sections)

        report = Report(
            user_id=user.id,
            section_id=section.id,
            shift=shift or None,
            description=description or None,
            image_file=image_file,
            status="pending",
            is_seen=False,
        )

        db.session.add(report)

        if not commit_changes():
            delete_uploaded_image(image_file)
            flash("ثبت گزارش انجام نشد.", "danger")
            return render_template("report_form.html", sections=sections)

        notify_section_reviewers(
            section.id,
            "گزارش نظافت جدید",
            f"{user.full_name} برای بخش «{section.name}» گزارش جدید ثبت کرد.",
            notification_type="report",
            target_url=url_for("main.report_detail", report_id=report.id),
            exclude_ids={user.id},
        )
        log_activity("ثبت گزارش", f"گزارش بخش {section.name} ثبت شد.")
        flash("گزارش با موفقیت ثبت شد.", "success")
        return dashboard_redirect(user)

    return render_template(
        "report_form.html",
        sections=sections,
        current_user=user,
    )


@main.route("/supply/add", methods=["GET", "POST"])
@role_required("staff")
def add_supply_usage():
    user = get_current_user()
    sections = list(user.sections)

    if request.method == "POST":
        section_id = request.form.get("section_id", type=int)
        shift = request.form.get("shift", "").strip()
        materials = request.form.get("materials", "").strip()
        equipment = request.form.get("equipment", "").strip()
        notes = request.form.get("notes", "").strip()
        estimated_cost = parse_non_negative_quantity(
            request.form.get("estimated_cost", "0")
        )

        section = db.session.get(Section, section_id)

        if not section or section not in user.sections:
            flash("بخش انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "supply_form.html",
                sections=sections,
                current_user=user,
            )

        if not shift:
            flash("شیفت را انتخاب کنید.", "danger")
            return render_template(
                "supply_form.html",
                sections=sections,
                current_user=user,
            )

        if estimated_cost is None:
            flash("هزینهٔ تقریبی باید عدد صفر یا بزرگ‌تر باشد.", "danger")
            return render_template(
                "supply_form.html",
                sections=sections,
                current_user=user,
            )

        if not materials and not equipment:
            flash("حداقل یک مورد از مواد یا تجهیزات را وارد کنید.", "danger")
            return render_template(
                "supply_form.html",
                sections=sections,
                current_user=user,
            )

        usage = SupplyUsage(
            user_id=user.id,
            section_id=section.id,
            shift=shift,
            materials=materials or "ثبت نشده",
            equipment=equipment or None,
            notes=notes or None,
            estimated_cost=estimated_cost,
            status="pending",
        )
        db.session.add(usage)

        if not commit_changes():
            flash("ثبت مصرف شیفت انجام نشد.", "danger")
            return render_template(
                "supply_form.html",
                sections=sections,
                current_user=user,
            )

        notify_section_reviewers(
            section.id,
            "مصرف شیفت جدید",
            f"{user.full_name} مصرف شیفت بخش «{section.name}» را ثبت کرد.",
            notification_type="supply",
            target_url=url_for("main.supervisor") + "#supply-usages",
            exclude_ids={user.id},
        )
        log_activity(
            "ثبت مصرف شیفت",
            f"مصرف مواد و تجهیزات بخش {section.name} ثبت شد.",
        )
        flash("مصرف مواد و تجهیزات برای مسئول بخش ارسال شد.", "success")
        return redirect(url_for("main.staff"))

    return render_template(
        "supply_form.html",
        sections=sections,
        current_user=user,
    )


@main.route("/inventory", methods=["GET", "POST"])
@role_required("supervisor", "observer")
def inventory():
    user = get_current_user()
    sections = (
        Section.query.order_by(Section.name.asc()).all()
        if user.role == "observer"
        else sorted(user.sections, key=lambda section: section.name)
    )

    def render_inventory_page():
        return render_template(
            "inventory.html",
            sections=sections,
            inventory_items=visible_inventory_for(user),
            inventory_categories=INVENTORY_CATEGORIES,
            can_manage=user.role == "supervisor",
            current_user=user,
        )

    if request.method == "POST":
        if user.role == "observer":
            flash("ناظر اداره‌کل فقط می‌تواند موجودی‌ها را مشاهده کند.", "warning")
            return redirect(url_for("main.inventory"))

        section_id = request.form.get("section_id", type=int)
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "material").strip()
        unit = request.form.get("unit", "عدد").strip()
        current_quantity = parse_non_negative_quantity(
            request.form.get("current_quantity")
        )
        minimum_quantity = parse_non_negative_quantity(
            request.form.get("minimum_quantity")
        )
        section = db.session.get(Section, section_id)

        if not section or section not in user.sections:
            flash("بخش انتخاب‌شده معتبر نیست.", "danger")
            return render_inventory_page()

        if not name:
            flash("نام قلم موجودی را وارد کنید.", "danger")
            return render_inventory_page()

        if category not in INVENTORY_CATEGORIES:
            flash("نوع قلم موجودی معتبر نیست.", "danger")
            return render_inventory_page()

        if not unit:
            flash("واحد اندازه‌گیری را وارد کنید.", "danger")
            return render_inventory_page()

        if current_quantity is None or minimum_quantity is None:
            flash("موجودی فعلی و حد هشدار باید عدد صفر یا بزرگ‌تر باشند.", "danger")
            return render_inventory_page()

        duplicate = (
            SupplyInventory.query
            .filter_by(
                section_id=section.id,
                name=name,
                category=category,
                is_active=True,
            )
            .first()
        )
        if duplicate:
            flash("این قلم قبلاً برای همین بخش ثبت شده است.", "warning")
            return render_inventory_page()

        inventory_item = SupplyInventory(
            section_id=section.id,
            created_by=user.id,
            name=name,
            category=category,
            unit=unit,
            current_quantity=current_quantity,
            minimum_quantity=minimum_quantity,
            is_active=True,
        )
        db.session.add(inventory_item)
        db.session.add(
            InventorySnapshot(
                item=inventory_item,
                quantity=current_quantity,
                source="initial",
                note="موجودی اولیه",
                recorded_by=user.id,
            )
        )

        if commit_changes():
            log_activity(
                "ثبت موجودی بخش",
                f"قلم {name} برای بخش {section.name} ثبت شد.",
            )
            flash("قلم موجودی با موفقیت ثبت شد.", "success")
            return redirect(url_for("main.inventory"))

        flash("ثبت موجودی انجام نشد.", "danger")

    return render_inventory_page()


@main.route("/inventory/<int:item_id>/update", methods=["POST"])
@role_required("supervisor")
def update_inventory(item_id):
    user = get_current_user()
    item = db.session.get(SupplyInventory, item_id)
    section_ids = {section.id for section in user.sections}

    if not item or not item.is_active or item.section_id not in section_ids:
        flash("قلم موجودی پیدا نشد یا به شما مربوط نیست.", "danger")
        return redirect(url_for("main.inventory"))

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "material").strip()
    unit = request.form.get("unit", "عدد").strip()
    current_quantity = parse_non_negative_quantity(
        request.form.get("current_quantity")
    )
    minimum_quantity = parse_non_negative_quantity(
        request.form.get("minimum_quantity")
    )

    if (
        not name
        or category not in INVENTORY_CATEGORIES
        or not unit
        or current_quantity is None
        or minimum_quantity is None
    ):
        flash("اطلاعات موجودی معتبر نیست.", "danger")
        return redirect(url_for("main.inventory"))

    duplicate = (
        SupplyInventory.query
        .filter(
            SupplyInventory.id != item.id,
            SupplyInventory.section_id == item.section_id,
            SupplyInventory.name == name,
            SupplyInventory.category == category,
            SupplyInventory.is_active.is_(True),
        )
        .first()
    )
    if duplicate:
        flash("قلم مشابهی برای این بخش وجود دارد.", "warning")
        return redirect(url_for("main.inventory"))

    item.name = name
    item.category = category
    item.unit = unit
    item.current_quantity = current_quantity
    item.minimum_quantity = minimum_quantity
    db.session.add(
        InventorySnapshot(
            item=item,
            quantity=current_quantity,
            source="manual",
            note="به‌روزرسانی موجودی",
            recorded_by=user.id,
        )
    )

    if commit_changes():
        log_activity(
            "به‌روزرسانی موجودی بخش",
            f"موجودی {item.name} به‌روزرسانی شد.",
        )
        flash("موجودی با موفقیت به‌روزرسانی شد.", "success")
    else:
        flash("به‌روزرسانی موجودی انجام نشد.", "danger")

    return redirect(url_for("main.inventory"))


@main.route("/inventory/<int:item_id>/delete", methods=["POST"])
@role_required("supervisor")
def delete_inventory(item_id):
    user = get_current_user()
    item = db.session.get(SupplyInventory, item_id)
    section_ids = {section.id for section in user.sections}

    if not item or item.section_id not in section_ids:
        flash("قلم موجودی پیدا نشد یا به شما مربوط نیست.", "danger")
        return redirect(url_for("main.inventory"))

    item.is_active = False
    if commit_changes():
        log_activity(
            "حذف قلم موجودی",
            f"قلم {item.name} از موجودی غیرفعال شد.",
        )
        flash("قلم موجودی حذف شد.", "success")
    else:
        flash("حذف قلم موجودی انجام نشد.", "danger")

    return redirect(url_for("main.inventory"))


@main.route("/reports/<int:report_id>/resubmit", methods=["GET", "POST"])
@role_required("staff")
def resubmit_report(report_id):
    user = get_current_user()
    original = db.session.get(Report, report_id)

    if not original or original.user_id != user.id:
        flash("گزارش موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.staff"))

    if original.status != "rejected":
        flash("فقط گزارش ردشده امکان ثبت مجدد دارد.", "warning")
        return redirect(url_for("main.report_detail", report_id=original.id))

    sections = list(user.sections)

    if request.method == "POST":
        section_id = request.form.get("section_id", type=int)
        shift = request.form.get("shift", "").strip()
        description = request.form.get("description", "").strip()
        image = request.files.get("image")

        section = db.session.get(Section, section_id)
        if not section or section not in user.sections:
            flash("بخش انتخاب‌شده معتبر نیست.", "danger")
            return render_template(
                "report_resubmit.html",
                original=original,
                sections=sections,
                current_user=user,
            )

        if not shift or not description:
            flash("شیفت و شرح فعالیت را کامل کنید.", "danger")
            return render_template(
                "report_resubmit.html",
                original=original,
                sections=sections,
                current_user=user,
            )

        image_file = None
        if image and image.filename:
            image_file = save_uploaded_image(image)
            if not image_file:
                flash("فرمت تصویر مجاز نیست.", "danger")
                return render_template(
                    "report_resubmit.html",
                    original=original,
                    sections=sections,
                    current_user=user,
                )
        else:
            image_file = copy_uploaded_image(original.image_file)

        replacement = Report(
            user_id=user.id,
            section_id=section.id,
            shift=shift,
            description=description,
            image_file=image_file,
            status=RESUBMITTED_STATUS,
            is_seen=False,
        )

        try:
            db.session.add(replacement)
            db.session.flush()
            db.session.add(
                ReportResubmission(
                    original_report_id=original.id,
                    replacement_report_id=replacement.id,
                )
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            delete_uploaded_image(image_file)
            flash("ثبت مجدد گزارش انجام نشد.", "danger")
            return render_template(
                "report_resubmit.html",
                original=original,
                sections=sections,
                current_user=user,
            )

        log_activity(
            "ثبت مجدد گزارش",
            f"گزارش جدید بر اساس گزارش ردشده شماره {original.id} ثبت شد.",
        )
        notify_section_reviewers(
            replacement.section_id,
            "گزارش ثبت مجدد شد",
            f"{user.full_name} گزارش ردشدهٔ بخش «{replacement.section.name}» را اصلاح و دوباره ثبت کرد.",
            notification_type="report",
            target_url=url_for(
                "main.report_detail",
                report_id=replacement.id,
            ),
            exclude_ids={user.id},
        )
        flash("گزارش مجدداً ثبت شد و برای مسئول بخش ارسال گردید.", "success")
        return redirect(url_for("main.staff"))

    return render_template(
        "report_resubmit.html",
        original=original,
        sections=sections,
        current_user=user,
    )


@main.route("/supplies/<int:usage_id>/review", methods=["POST"])
@role_required("manager", "supervisor")
def review_supply_usage(usage_id):
    user = get_current_user()
    usage = db.session.get(SupplyUsage, usage_id)

    if not usage:
        flash("گزارش مصرف موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.supervisor"))

    if user.role == "supervisor":
        section_ids = {section.id for section in user.sections}
        if usage.section_id not in section_ids:
            flash("شما اجازه بررسی این گزارش مصرف را ندارید.", "danger")
            return redirect(url_for("main.supervisor"))

    status = request.form.get("status", "reviewed").strip()
    feedback = request.form.get("supervisor_feedback", "").strip()

    if status not in ALLOWED_SUPPLY_STATUSES:
        flash("وضعیت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.supervisor"))

    usage.status = status
    usage.supervisor_feedback = feedback or None
    usage.reviewed_by = user.id if status == "reviewed" else None
    usage.reviewed_at = utc_now() if status == "reviewed" else None

    if commit_changes():
        create_notifications(
            [usage.user_id],
            "وضعیت مصرف شیفت تغییر کرد",
            f"گزارش مصرف شیفت بخش «{usage.section.name}» بررسی شد.",
            notification_type="supply",
            target_url=url_for("main.staff"),
        )
        log_activity(
            "بررسی مصرف شیفت",
            f"گزارش مصرف شماره {usage.id} بررسی شد.",
        )
        flash("وضعیت گزارش مصرف با موفقیت ذخیره شد.", "success")
    else:
        flash("ذخیره وضعیت گزارش مصرف انجام نشد.", "danger")

    return redirect(url_for("main.supervisor"))


@main.route("/supply-report")
@role_required("supervisor", "observer")
def supply_report():
    user = get_current_user()
    month_value, start, end, month_label = selected_month_range(
        request.args.get("jalali_month")
        or request.args.get("month", "")
    )
    usages = [
        usage
        for usage in visible_supply_usages_for(user)
        if usage.created_at and start <= usage.created_at < end
    ]

    if user.role in {"manager", "observer"}:
        sections = Section.query.order_by(Section.name.asc()).all()
    else:
        sections = sorted(user.sections, key=lambda section: section.name)

    section_data = supply_section_summary(usages, sections)

    return render_template(
        "supply_report.html",
        usages=usages,
        section_data=section_data,
        selected_month=month_value,
        selected_month_label=month_label,
        current_user=user,
    )


@main.route("/supply-report/export")
@role_required("supervisor", "observer")
def export_supply_report():
    user = get_current_user()
    month_value, start, end, _ = selected_month_range(
        request.args.get("jalali_month")
        or request.args.get("month", "")
    )
    usages = [
        usage
        for usage in visible_supply_usages_for(user)
        if usage.created_at and start <= usage.created_at < end
    ]

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        [
            "شماره",
            "تاریخ ثبت",
            "ثبت‌کننده",
            "بخش",
            "شیفت",
            "مواد شوینده و مصرفی",
            "لوازم و تجهیزات",
            "هزینه تقریبی (تومان)",
            "توضیحات",
            "وضعیت بررسی",
            "بازخورد مسئول بخش",
        ]
    )

    status_names = {
        "pending": "در انتظار بررسی",
        "reviewed": "بررسی شده",
    }
    for usage in usages:
        writer.writerow(
            [
                usage.id,
                format_date_for_user(user, usage.created_at)
                if usage.created_at
                else "",
                usage.user.full_name if usage.user else "",
                usage.section.name if usage.section else "",
                usage.shift,
                usage.materials or "",
                usage.equipment or "",
                getattr(usage, "estimated_cost", 0) or 0,
                usage.notes or "",
                status_names.get(usage.status, usage.status or ""),
                usage.supervisor_feedback or "",
            ]
        )

    response = current_app.response_class(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = (
        f'attachment; filename="supply-report-{month_value}.csv"'
    )
    log_activity(
        "خروجی ماهانه مصرف",
        f"خروجی مصرف ماه {month_value} دریافت شد.",
    )
    return response


def reports_export_scope(user):
    reports = visible_reports_for(user)
    requested_month = request.args.get("jalali_month", "").strip()
    if requested_month:
        month_value, start, end, month_label = selected_month_range(
            requested_month
        )
        reports = [
            report
            for report in reports
            if report.created_at and start <= report.created_at < end
        ]
    else:
        month_value = "all"
        month_label = "همهٔ بازه‌ها"

    scope_label = "همهٔ بخش‌ها" if user.role in {"manager", "observer"} else "بخش‌های تحت نظارت"
    return reports, month_value, month_label, scope_label


def supply_export_scope(user):
    month_value, start, end, month_label = selected_month_range(
        request.args.get("jalali_month")
        or request.args.get("month", "")
    )
    usages = [
        usage
        for usage in visible_supply_usages_for(user)
        if usage.created_at and start <= usage.created_at < end
    ]
    sections = (
        Section.query.order_by(Section.name.asc()).all()
        if user.role in {"manager", "observer"}
        else sorted(user.sections, key=lambda section: section.name)
    )
    scope_label = "همهٔ بخش‌ها" if user.role in {"manager", "observer"} else "بخش‌های تحت نظارت"
    return (
        usages,
        supply_section_summary(usages, sections),
        month_value,
        month_label,
        scope_label,
    )


@main.route("/reports/export.xlsx")
@role_required("manager", "supervisor", "observer")
def export_reports_xlsx():
    user = get_current_user()
    reports, month_value, month_label, scope_label = reports_export_scope(user)
    output = build_reports_xlsx(
        reports,
        month_label,
        scope_label,
        date_formatter=lambda value: format_date_for_user(user, value),
    )
    log_activity("خروجی Excel گزارش‌ها", f"بازه {month_value} دریافت شد.")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pakyar-reports-{month_value}.xlsx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )


@main.route("/reports/export.pdf")
@role_required("manager", "supervisor", "observer")
def export_reports_pdf():
    user = get_current_user()
    reports, month_value, month_label, scope_label = reports_export_scope(user)
    output = build_reports_pdf(
        reports,
        month_label,
        scope_label,
        font_dir=Path(current_app.root_path) / "static" / "fonts",
        date_formatter=lambda value: format_date_for_user(user, value),
    )
    log_activity("خروجی PDF گزارش‌ها", f"بازه {month_value} دریافت شد.")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pakyar-reports-{month_value}.pdf",
        mimetype="application/pdf",
    )


@main.route("/supply-report/export.xlsx")
@role_required("supervisor", "observer")
def export_supply_report_xlsx():
    user = get_current_user()
    usages, section_data, month_value, month_label, scope_label = (
        supply_export_scope(user)
    )
    output = build_supply_xlsx(
        usages,
        section_data,
        month_label,
        scope_label,
        date_formatter=lambda value: format_date_for_user(user, value),
    )
    log_activity(
        "خروجی Excel هزینه و مصرف",
        f"خروجی مصرف ماه {month_value} دریافت شد.",
    )
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pakyar-supply-{month_value}.xlsx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )


@main.route("/supply-report/export.pdf")
@role_required("supervisor", "observer")
def export_supply_report_pdf():
    user = get_current_user()
    usages, section_data, month_value, month_label, scope_label = (
        supply_export_scope(user)
    )
    output = build_supply_pdf(
        usages,
        section_data,
        month_label,
        scope_label,
        font_dir=Path(current_app.root_path) / "static" / "fonts",
        date_formatter=lambda value: format_date_for_user(user, value),
    )
    log_activity(
        "خروجی PDF هزینه و مصرف",
        f"خروجی مصرف ماه {month_value} دریافت شد.",
    )
    return send_file(
        output,
        as_attachment=True,
        download_name=f"pakyar-supply-{month_value}.pdf",
        mimetype="application/pdf",
    )


@main.route("/reports/<int:report_id>")
@login_required
def report_detail(report_id):
    user = get_current_user()
    report = db.session.get(Report, report_id)

    if not report:
        flash("گزارش موردنظر پیدا نشد.", "danger")
        return dashboard_redirect(user)

    if user.role == "staff" and report.user_id != user.id:
        flash("شما اجازه مشاهده این گزارش را ندارید.", "danger")
        return dashboard_redirect(user)

    if user.role == "supervisor":
        section_ids = {section.id for section in user.sections}

        if report.section_id not in section_ids:
            flash("شما اجازه مشاهده این گزارش را ندارید.", "danger")
            return dashboard_redirect(user)

    if user.role in {"manager", "supervisor"} and not report.is_seen:
        report.is_seen = True
        commit_changes()

    return render_template(
        "report_detail.html",
        report=report,
        current_user=user,
    )


@main.route("/reports/<int:report_id>/status", methods=["POST"])
@role_required("manager", "supervisor")
def update_report_status(report_id):
    user = get_current_user()
    report = db.session.get(Report, report_id)

    if not report:
        flash("گزارش موردنظر پیدا نشد.", "danger")
        return dashboard_redirect(user)

    if user.role == "supervisor":
        section_ids = {section.id for section in user.sections}

        if report.section_id not in section_ids:
            flash("شما اجازه تغییر این گزارش را ندارید.", "danger")
            return dashboard_redirect(user)

    status = request.form.get("status", "").strip()
    feedback = request.form.get("supervisor_feedback", "").strip()

    if status not in ALLOWED_STATUSES:
        flash("وضعیت انتخاب‌شده معتبر نیست.", "danger")
        return redirect(url_for("main.report_detail", report_id=report.id))
    if status == "rejected" and not feedback:
        flash("برای رد کردن گزارش، توضیح مسئول بخش الزامی است.", "danger")
        return redirect(url_for("main.report_detail", report_id=report.id))

    report.status = status
    report.supervisor_feedback = feedback or None
    report.reviewed_by = user.id
    report.reviewed_at = utc_now()
    report.is_seen = True

    if commit_changes():
        create_notifications(
            [report.user_id],
            "وضعیت گزارش تغییر کرد",
            f"گزارش شمارهٔ {report.id} اکنون «{report_status_label(report)}» است.",
            notification_type="report",
            target_url=url_for(
                "main.report_detail",
                report_id=report.id,
            ),
        )
        log_activity(
            "تغییر وضعیت گزارش",
            f"وضعیت گزارش شماره {report.id} به {status} تغییر کرد.",
        )
        flash("وضعیت گزارش با موفقیت تغییر کرد.", "success")
    else:
        flash("تغییر وضعیت گزارش انجام نشد.", "danger")

    return redirect(url_for("main.report_detail", report_id=report.id))


@main.route("/reports/<int:report_id>/delete", methods=["POST"])
@role_required("manager")
def delete_report(report_id):
    report = db.session.get(Report, report_id)

    if not report:
        flash("گزارش موردنظر پیدا نشد.", "danger")
        return redirect(url_for("main.manager"))

    image_file = report.image_file
    report_id_value = report.id

    db.session.delete(report)

    if commit_changes():
        delete_uploaded_image(image_file)
        log_activity("حذف گزارش", f"گزارش شماره {report_id_value} حذف شد.")
        flash("گزارش با موفقیت حذف شد.", "success")
    else:
        flash("حذف گزارش انجام نشد.", "danger")

    return redirect(url_for("main.manager"))


@main.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(get_upload_folder(), filename)


@main.route("/export_reports")
@role_required("manager", "supervisor", "observer")
def export_reports():
    user = get_current_user()

    if user.role in ELEVATED_ROLES:
        reports = Report.query.order_by(Report.created_at.desc()).all()
    else:
        section_ids = [section.id for section in user.sections]
        reports = (
            Report.query.filter(Report.section_id.in_(section_ids))
            .order_by(Report.created_at.desc())
            .all()
            if section_ids
            else []
        )

    output = io.StringIO()
    output.write("\ufeff")

    writer = csv.writer(output)
    writer.writerow(
        [
            "شماره",
            "ثبت‌کننده",
            "بخش",
            "شیفت",
            "توضیحات",
            "وضعیت",
            "بازخورد",
            "تاریخ ثبت",
        ]
    )

    for report in reports:
        writer.writerow(
            [
                report.id,
                report.user.full_name if report.user else "",
                report.section.name if report.section else "",
                report.shift or "",
                report.description or "",
                report_status_label(report),
                report.supervisor_feedback or "",
                format_date_for_user(user, report.created_at)
                if report.created_at
                else "",
            ]
        )

    response = current_app.response_class(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = (
        "attachment; filename=reports.csv"
    )

    log_activity("خروجی گزارش‌ها", "فایل گزارش‌ها دریافت شد.")
    return response


@main.route("/logs")
@role_required("manager", "observer")
def logs():
    activities = (
        ActivityLog.query
        .order_by(ActivityLog.created_at.desc())
        .all()
    )

    return render_template(
        "logs.html",
        logs=activities,
        activities=activities,
        current_user=get_current_user(),
    )


@main.route("/backups")
@role_required("manager")
def backups():
    return render_template(
        "backups.html",
        backups=list_backups(current_app),
        latest_backup=latest_backup(current_app),
        current_user=get_current_user(),
    )


@main.route("/backups/create", methods=["POST"])
@role_required("manager")
def create_manual_backup():
    try:
        path = create_backup(current_app._get_current_object(), reason="manual")
    except Exception:
        current_app.logger.exception("Manual PakYar backup failed")
        flash("ایجاد پشتیبان انجام نشد. گزارش خطا در لاگ ثبت شد.", "danger")
        return redirect(url_for("main.backups"))

    log_activity("ایجاد پشتیبان", f"پشتیبان {path.name} ایجاد شد.")
    flash("پشتیبان جدید با موفقیت ایجاد شد.", "success")
    return redirect(url_for("main.backups"))


@main.route("/backups/download/<path:filename>")
@role_required("manager")
def download_backup(filename):
    return send_from_directory(
        backup_directory(current_app._get_current_object()),
        filename,
        as_attachment=True,
    )


@main.route("/backups/validate/<path:filename>", methods=["POST"])
@role_required("manager")
def validate_backup_file(filename):
    result = validate_backup(current_app._get_current_object(), filename)
    if result["valid"]:
        flash(
            f"پشتیبان {result['name']} سالم است؛ "
            f"{result['uploads']} فایل پیوست داخل آن وجود دارد.",
            "success",
        )
    else:
        details = " ".join(result["errors"]) or "ساختار پشتیبان کامل نیست."
        flash(f"اعتبارسنجی پشتیبان ناموفق بود: {details}", "danger")
    return redirect(url_for("main.backups"))


@main.app_context_processor
def inject_current_user():
    user = get_current_user()
    unread_message_count = 0
    unread_notification_count = 0

    if user and user.role not in ELEVATED_ROLES:
        unread_message_count = (
            ManagerMessage.query
            .filter_by(recipient_id=user.id, is_seen=False)
            .count()
        )
    if user:
        unread_notification_count = (
            Notification.query
            .filter_by(user_id=user.id, is_read=False)
            .count()
        )

    return {
        "current_user": user,
        "unread_message_count": unread_message_count,
        "unread_notification_count": unread_notification_count,
        "message_status_label": message_status_label,
        "message_priority_label": message_priority_label,
        "report_status_label": report_status_label,
        "role_label": role_label,
        "active_observer_exists": active_observer_exists,
        "rule_assigned_count": lambda rule: len(reminder_assigned_ids(rule)),
        "csrf_token": csrf_token,
    }
