import json
import threading
import time
from datetime import timedelta

from core.models import Notification, ReminderEvent, ReminderRule, db
from core.time_utils import iran_now, utc_now

_lock = threading.Lock()
_started = False


def _active_weekday(rule, now_local):
    selected = {item.strip() for item in (rule.weekdays or "all").split(",") if item.strip()}
    if not selected or "all" in selected:
        return True
    codes = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return codes[now_local.weekday()] in selected


def _inside_time_window(rule, now_local):
    if not rule.start_time and not rule.end_time:
        return True
    current = now_local.strftime("%H:%M")
    start = rule.start_time or "00:00"
    end = rule.end_time or "23:59"
    return start <= current <= end if start <= end else current >= start or current <= end


def _assigned_ids(rule):
    try:
        values = json.loads(rule.assigned_user_ids or "[]")
    except (TypeError, ValueError):
        values = []
    return {int(value) for value in values if str(value).isdigit()}


def process_due_reminders(app):
    with app.app_context():
        now = utc_now()
        local_now = iran_now()
        from core.wellbeing_routes import notify_due_actions
        notify_due_actions()
        rules = ReminderRule.query.filter(ReminderRule.is_active.is_(True), ReminderRule.next_due_at <= now).all()
        for rule in rules:
            if not _active_weekday(rule, local_now) or not _inside_time_window(rule, local_now):
                rule.next_due_at = now + timedelta(minutes=max(rule.interval_minutes, 5))
                continue
            due_at = rule.next_due_at
            for user_id in _assigned_ids(rule):
                exists = ReminderEvent.query.filter_by(rule_id=rule.id, assigned_user_id=user_id, due_at=due_at).first()
                if exists:
                    continue
                db.session.add(ReminderEvent(rule_id=rule.id, assigned_user_id=user_id, section_id=rule.section_id, due_at=due_at))
                db.session.add(Notification(user_id=user_id, title=f"یادآوری: {rule.title}", body=rule.description or f"زمان انجام کار در بخش {rule.section.name} فرا رسیده است.", notification_type="reminder", target_url="/reminders"))
            rule.last_triggered_at = now
            rule.next_due_at = now + timedelta(minutes=max(rule.interval_minutes, 5))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Reminder scheduler failed")


def start_reminder_scheduler(app):
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def worker():
        while True:
            try:
                process_due_reminders(app)
            except Exception:
                app.logger.exception("Reminder scheduler crashed")
            time.sleep(30)

    threading.Thread(target=worker, name="pakyar-reminder-scheduler", daemon=True).start()
