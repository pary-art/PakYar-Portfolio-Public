"""Additive workload tables; existing reports and accounts are not rewritten."""
from core.models import db
from core.time_utils import utc_now


class WorkProfile(db.Model):
    __tablename__ = 'work_profiles'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    height_cm = db.Column(db.Float)
    weight_kg = db.Column(db.Float)
    birth_date = db.Column(db.Date)
    accommodations = db.Column(db.JSON)
    consented_at = db.Column(db.DateTime)
    consent_recorded_by = db.Column(db.Integer)
    restrictions = db.Column(db.String(1000), nullable=False, default='')
    reference = db.Column(db.String(300), nullable=False, default='')
    review_due = db.Column(db.Date)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    user = db.relationship('User', backref=db.backref('work_profile', uselist=False, cascade='all, delete-orphan'))


class WorkSection(db.Model):
    __tablename__ = 'work_sections'
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), primary_key=True)
    area = db.Column(db.Float, nullable=False)
    beds = db.Column(db.Integer, nullable=False)
    occupied_beds = db.Column(db.Integer, nullable=False)
    toilets = db.Column(db.Integer, nullable=False)
    bins = db.Column(db.Integer, nullable=False)
    isolation_rooms = db.Column(db.Integer, nullable=False)
    equipment = db.Column(db.String(1000), nullable=False, default='')
    ergonomics = db.Column(db.JSON)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    section = db.relationship('Section', backref=db.backref('work_spec', uselist=False, cascade='all, delete-orphan'))


class WorkTask(db.Model):
    __tablename__ = 'work_tasks'
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    unit = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    repeats = db.Column(db.Float, nullable=False)
    minutes = db.Column(db.Float, nullable=False)
    shift = db.Column(db.String(20), nullable=False)
    weekday = db.Column(db.Integer)  # Python weekday, None = every day
    basis = db.Column(db.String(20), nullable=False)
    reference = db.Column(db.String(500), nullable=False)
    approved_by = db.Column(db.String(150), nullable=False, default='')
    approved_date = db.Column(db.Date)
    needs_review = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    section = db.relationship('Section', backref=db.backref('work_tasks', cascade='all, delete-orphan'))


class WorkCapacity(db.Model):
    __tablename__ = 'work_capacities'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    work_date = db.Column(db.Date, nullable=False)
    shift = db.Column(db.String(20), nullable=False)
    shift_minutes = db.Column(db.Float, nullable=False)
    break_minutes = db.Column(db.Float, nullable=False)
    other_minutes = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'work_date', 'shift'),)
    user = db.relationship('User', backref=db.backref('work_capacities', cascade='all, delete-orphan'))

    @property
    def available(self):
        return self.shift_minutes - self.break_minutes - self.other_minutes


class WorkPlan(db.Model):
    __tablename__ = 'work_plans'
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='CASCADE'), nullable=False)
    work_date = db.Column(db.Date, nullable=False)
    shift = db.Column(db.String(20), nullable=False)
    total_minutes = db.Column(db.Float, nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    __table_args__ = (db.UniqueConstraint('section_id', 'work_date', 'shift'),)
    section = db.relationship('Section', backref=db.backref('work_plans', cascade='all, delete-orphan'))


class WorkAllocation(db.Model):
    __tablename__ = 'work_allocations'
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('work_plans.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    user_name = db.Column(db.String(120), nullable=False)
    percent = db.Column(db.Float, nullable=False)
    minutes = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint('plan_id', 'user_id'),)
    plan = db.relationship('WorkPlan', backref=db.backref('allocations', cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('work_allocations'))
