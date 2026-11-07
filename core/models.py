from flask_sqlalchemy import SQLAlchemy
from core.time_utils import utc_now

db = SQLAlchemy()

# جدول واسط ارتباط چند-به-چند بین پرسنل و بخش‌ها
user_sections = db.Table(
    'user_sections',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('section_id', db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), primary_key=True)
)

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False, default="کاربر سامانه")
    role = db.Column(db.String(20), nullable=False, default="staff")  # manager, supervisor, observer, staff
    # Only the user explicitly appointed by the observer can use manager tools.
    is_appointed_manager = db.Column(db.Boolean, nullable=False, default=False)
    theme = db.Column(db.String(30), nullable=False, default="ocean")
    font_scale = db.Column(db.String(20), nullable=False, default="normal")
    date_mode = db.Column(db.String(20), nullable=False, default="jalali")
    compact_mode = db.Column(db.Boolean, nullable=False, default=False)
    reduced_motion = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    # ارتباطات
    sections = db.relationship('Section', secondary=user_sections, backref=db.backref('assigned_users', lazy='dynamic'))
    reports = db.relationship('Report', backref='user', lazy=True, cascade='all, delete-orphan')

class Section(db.Model):
    __tablename__ = 'sections'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    floor = db.Column(db.String(50), nullable=True)
    description = db.Column(db.String(255), nullable=True)

    reports = db.relationship('Report', backref='section', lazy=True, cascade='all, delete-orphan')

class Report(db.Model):
    __tablename__ = 'reports'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), nullable=False)
    shift = db.Column(db.String(50), nullable=True)  # صبح، عصر، شب
    description = db.Column(db.Text, nullable=True)
    image_file = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    supervisor_feedback = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(db.Integer, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    is_seen = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utc_now)


class ReportResubmission(db.Model):
    __tablename__ = 'report_resubmissions'

    id = db.Column(db.Integer, primary_key=True)
    original_report_id = db.Column(
        db.Integer,
        db.ForeignKey('reports.id', ondelete='CASCADE'),
        nullable=False,
    )
    replacement_report_id = db.Column(
        db.Integer,
        db.ForeignKey('reports.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
    )
    created_at = db.Column(db.DateTime, default=utc_now)

    original_report = db.relationship(
        'Report',
        foreign_keys=[original_report_id],
        backref=db.backref('resubmissions', lazy=True),
    )
    replacement_report = db.relationship(
        'Report',
        foreign_keys=[replacement_report_id],
        backref=db.backref('resubmission_source', uselist=False),
    )


class SupplyUsage(db.Model):
    __tablename__ = 'supply_usages'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    section_id = db.Column(
        db.Integer,
        db.ForeignKey('sections.id', ondelete='CASCADE'),
        nullable=False,
    )
    shift = db.Column(db.String(50), nullable=False)
    materials = db.Column(db.Text, nullable=False)
    equipment = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    estimated_cost = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default='pending')
    supervisor_feedback = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('supply_usages', lazy=True),
    )
    section = db.relationship(
        'Section',
        foreign_keys=[section_id],
        backref=db.backref('supply_usages', lazy=True),
    )
    reviewer = db.relationship(
        'User',
        foreign_keys=[reviewed_by],
    )


class SupplyInventory(db.Model):
    """موجودی مواد شوینده، مصرفی و تجهیزات هر بخش."""

    __tablename__ = 'supply_inventory'

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(
        db.Integer,
        db.ForeignKey('sections.id', ondelete='CASCADE'),
        nullable=False,
    )
    created_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(20), nullable=False, default='material')
    unit = db.Column(db.String(30), nullable=False, default='عدد')
    current_quantity = db.Column(db.Float, nullable=False, default=0)
    minimum_quantity = db.Column(db.Float, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    section = db.relationship(
        'Section',
        foreign_keys=[section_id],
        backref=db.backref('supply_inventory', lazy=True),
    )
    creator = db.relationship(
        'User',
        foreign_keys=[created_by],
        backref=db.backref('created_supply_inventory', lazy=True),
    )


class InventorySnapshot(db.Model):
    """ثبت تاریخچهٔ مقدار موجودی برای محاسبهٔ روند مصرف."""

    __tablename__ = 'inventory_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(
        db.Integer,
        db.ForeignKey('supply_inventory.id', ondelete='CASCADE'),
        nullable=False,
    )
    quantity = db.Column(db.Float, nullable=False, default=0)
    source = db.Column(db.String(30), nullable=False, default='manual')
    note = db.Column(db.String(255), nullable=True)
    recorded_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    recorded_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    item = db.relationship(
        'SupplyInventory',
        foreign_keys=[item_id],
        backref=db.backref(
            'snapshots',
            lazy=True,
            cascade='all, delete-orphan',
        ),
    )
    recorder = db.relationship(
        'User',
        foreign_keys=[recorded_by],
    )


class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)


class ManagerMessage(db.Model):
    """پیام و پیگیری مشکلی که مسئول خدمات برای یکی از کاربران ارسال می‌کند."""

    __tablename__ = 'manager_messages'

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    recipient_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    section_id = db.Column(
        db.Integer,
        db.ForeignKey('sections.id', ondelete='SET NULL'),
        nullable=True,
    )
    subject = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=False, default='normal')
    status = db.Column(db.String(20), nullable=False, default='new')
    response = db.Column(db.Text, nullable=True)
    is_seen = db.Column(db.Boolean, nullable=False, default=False)
    responded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    sender = db.relationship(
        'User',
        foreign_keys=[sender_id],
        backref=db.backref('sent_messages', lazy=True),
    )
    recipient = db.relationship(
        'User',
        foreign_keys=[recipient_id],
        backref=db.backref('received_messages', lazy=True),
    )
    section = db.relationship(
        'Section',
        foreign_keys=[section_id],
        backref=db.backref('manager_messages', lazy=True),
    )


class Notification(db.Model):
    """اعلان داخلی برای کاربر؛ مستقل از پیام‌های پیگیری مسئول خدمات."""

    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    title = db.Column(db.String(180), nullable=False)
    body = db.Column(db.Text, nullable=True)
    notification_type = db.Column(
        db.String(20),
        nullable=False,
        default='info',
    )
    target_url = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref(
            'notifications',
            lazy=True,
            cascade='all, delete-orphan',
        ),
    )


class ReminderRule(db.Model):
    """قانون یادآوری تکرارشونده برای یک بخش و یک یا چند پرسنل."""

    __tablename__ = 'reminder_rules'

    id = db.Column(db.Integer, primary_key=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    interval_minutes = db.Column(db.Integer, nullable=False, default=120)
    start_time = db.Column(db.String(5), nullable=True)
    end_time = db.Column(db.String(5), nullable=True)
    weekdays = db.Column(db.String(120), nullable=True, default='all')
    assigned_user_ids = db.Column(db.Text, nullable=False, default='[]')
    next_due_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_triggered_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    creator = db.relationship('User', foreign_keys=[created_by], backref=db.backref('created_reminder_rules', lazy=True))
    section = db.relationship('Section', foreign_keys=[section_id], backref=db.backref('reminder_rules', lazy=True, cascade='all, delete-orphan'))


class ReminderEvent(db.Model):
    """یک نوبت واقعی از اجرای قانون یادآوری."""

    __tablename__ = 'reminder_events'
    __table_args__ = (
        db.UniqueConstraint('rule_id', 'assigned_user_id', 'due_at', name='uq_reminder_event_rule_user_due'),
    )

    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('reminder_rules.id', ondelete='CASCADE'), nullable=False)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    completed_at = db.Column(db.DateTime, nullable=True)
    completed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    rule = db.relationship('ReminderRule', foreign_keys=[rule_id], backref=db.backref('events', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[assigned_user_id], backref=db.backref('reminder_events', lazy=True))
    section = db.relationship('Section', foreign_keys=[section_id])
    completer = db.relationship('User', foreign_keys=[completed_by])


class PushSubscription(db.Model):
    """اشتراک اعلان مرورگر/PWA برای موبایل یا ویندوز."""

    __tablename__ = 'push_subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.Text, nullable=False)
    user_agent = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('push_subscriptions', lazy=True, cascade='all, delete-orphan'))


class ChecklistItem(db.Model):
    """یک مورد ثابت از چک‌لیست نظافت یک بخش و یک شیفت."""

    __tablename__ = 'checklist_items'

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(
        db.Integer,
        db.ForeignKey('sections.id', ondelete='CASCADE'),
        nullable=False,
    )
    created_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    shift = db.Column(db.String(20), nullable=False)
    frequency = db.Column(db.String(20), nullable=False, default='daily')
    # برای چک‌لیست هفتگی: saturday تا friday؛ برای چک‌لیست روزانه خالی است.
    weekday = db.Column(db.String(20), nullable=True)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    section = db.relationship(
        'Section',
        foreign_keys=[section_id],
        backref=db.backref('checklist_items', lazy=True),
    )
    creator = db.relationship(
        'User',
        foreign_keys=[created_by],
        backref=db.backref('created_checklist_items', lazy=True),
    )


class ChecklistCompletion(db.Model):
    """وضعیت انجام یک مورد چک‌لیست توسط پرسنل در یک روز."""

    __tablename__ = 'checklist_completions'
    __table_args__ = (
        db.UniqueConstraint(
            'item_id',
            'user_id',
            'period_key',
            'shift',
            name='uq_checklist_completion_item_user_period_shift',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(
        db.Integer,
        db.ForeignKey('checklist_items.id', ondelete='CASCADE'),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    checklist_date = db.Column(db.Date, nullable=False)
    period_key = db.Column(db.String(64), nullable=False, default='')
    shift = db.Column(db.String(20), nullable=False)
    is_completed = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(24), nullable=False, default='pending')
    note = db.Column(db.Text, nullable=True)
    image_file = db.Column(db.String(200), nullable=True)
    reviewed_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    item = db.relationship(
        'ChecklistItem',
        foreign_keys=[item_id],
        backref=db.backref('completions', lazy=True, cascade='all, delete-orphan'),
    )
    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('checklist_completions', lazy=True),
    )
    reviewer = db.relationship(
        'User',
        foreign_keys=[reviewed_by],
    )


class ShiftHandoff(db.Model):
    """یادداشت تحویل شیفت بین پرسنل و مسئول بخش."""

    __tablename__ = 'shift_handoffs'
    __table_args__ = (
        db.UniqueConstraint(
            'section_id',
            'user_id',
            'handoff_date',
            'shift',
            name='uq_shift_handoff_section_user_date_shift',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(
        db.Integer,
        db.ForeignKey('sections.id', ondelete='CASCADE'),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
    )
    handoff_date = db.Column(db.Date, nullable=False)
    shift = db.Column(db.String(20), nullable=False)
    note = db.Column(db.Text, nullable=False)
    is_acknowledged = db.Column(db.Boolean, nullable=False, default=False)
    acknowledged_by = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    acknowledged_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    section = db.relationship(
        'Section',
        foreign_keys=[section_id],
        backref=db.backref('shift_handoffs', lazy=True),
    )
    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('shift_handoffs', lazy=True),
    )
    acknowledger = db.relationship(
        'User',
        foreign_keys=[acknowledged_by],
    )
