"""Operational safety records and a separate private clinical database."""
from core.models import db
from core.time_utils import utc_now


class TaskAssessment(db.Model):
    __tablename__ = 'task_assessments'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, nullable=False, index=True)
    section_id = db.Column(db.Integer, nullable=False, index=True)
    task_title = db.Column(db.String(150), nullable=False)
    conditions = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(24), nullable=False)
    controls = db.Column(db.Text, nullable=False)
    reference = db.Column(db.String(500), nullable=False)
    reviewer_id = db.Column(db.Integer, nullable=False)
    reviewer_name = db.Column(db.String(120), nullable=False)
    reviewed_at = db.Column(db.DateTime, default=utc_now)
    review_due = db.Column(db.Date, nullable=False)


class ToolFit(db.Model):
    __tablename__ = 'tool_fits'
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    task_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    asset_code = db.Column(db.String(100), nullable=False)
    setting = db.Column(db.String(500), nullable=False)
    instruction = db.Column(db.Text, nullable=False)
    available = db.Column(db.Boolean, nullable=False)
    fit_status = db.Column(db.String(24), nullable=False, default='not_checked')
    image = db.Column(db.LargeBinary)
    examiner_id = db.Column(db.Integer, nullable=False)
    examiner_name = db.Column(db.String(120), nullable=False)
    review_due = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class FunctionalAdvice(db.Model):
    """Only work-relevant advice; no diagnosis or complaint body here."""
    __tablename__ = 'functional_advice'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    section_id = db.Column(db.Integer, nullable=False)
    advice = db.Column(db.Text, nullable=False)
    limits = db.Column(db.JSON, nullable=False)
    reference = db.Column(db.String(300), nullable=False)
    review_due = db.Column(db.Date, nullable=False)
    clinician_id = db.Column(db.Integer, nullable=False)
    clinician_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)


class ClinicalNote(db.Model):
    __bind_key__ = 'clinical'
    __tablename__ = 'clinical_notes'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    section_id = db.Column(db.Integer, nullable=False)
    clinician_id = db.Column(db.Integer, nullable=False)
    note = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)


class PrivateFeedback(db.Model):
    __bind_key__ = 'clinical'
    __tablename__ = 'private_feedback'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    section_id = db.Column(db.Integer, nullable=False)
    task_id = db.Column(db.Integer, nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='new')
    created_at = db.Column(db.DateTime, default=utc_now)


class FeedbackReply(db.Model):
    __bind_key__ = 'clinical'
    __tablename__ = 'feedback_replies'
    id = db.Column(db.Integer, primary_key=True)
    feedback_id = db.Column(db.Integer, nullable=False, index=True)
    author_id = db.Column(db.Integer, nullable=False)
    author_name = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)


class SafetyAction(db.Model):
    __tablename__ = 'safety_actions'
    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, nullable=False, index=True)
    task_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(180), nullable=False)
    before = db.Column(db.Text, nullable=False)
    target = db.Column(db.Text, nullable=False)
    responsible_id = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    created_by = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    status = db.Column(db.String(24), nullable=False, default='open')
    due_notified = db.Column(db.Boolean, nullable=False, default=False)


class SafetyReview(db.Model):
    __tablename__ = 'safety_reviews'
    id = db.Column(db.Integer, primary_key=True)
    action_id = db.Column(db.Integer, nullable=False, index=True)
    author_id = db.Column(db.Integer, nullable=False)
    author_name = db.Column(db.String(120), nullable=False)
    outcome = db.Column(db.String(24), nullable=False)
    result = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)
