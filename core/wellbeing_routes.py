"""Scoped occupational safety workflow; clinical content never enters general logs."""
from functools import wraps
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError

from core.models import ActivityLog, Notification, Section, User, db
from core.backups import latest_clinical_snapshot
from core.routes import enforce_session_timeout, get_current_user, login_required, protect_state_changing_requests
from core.time_utils import iran_now
from core.workload import date_label, number, parse_day
from core.ergonomics import MEASUREMENTS, CONDITIONS, LIMITS, parse_conditions, recommendations
from core.workload_models import WorkTask, WorkProfile
from core.workload_models import WorkSection
from core.wellbeing_models import (TaskAssessment, ToolFit, FunctionalAdvice, ClinicalNote,
                                   PrivateFeedback, FeedbackReply, SafetyAction, SafetyReview)

wellbeing = Blueprint('wellbeing', __name__, url_prefix='/wellbeing')
wellbeing.before_request(enforce_session_timeout)
wellbeing.before_request(protect_state_changing_requests)
EXPERTS = ('manager',)
STATUS = {'incomplete':'اطلاعات ناکافی', 'changes':'نیازمند اصلاح', 'reviewed':'بررسی‌شده توسط کارشناس'}
OUTCOMES = {'open':'باز', 'implemented':'انجام‌شده؛ منتظر ارزیابی', 'effective':'اثربخش؛ بسته‌شده', 'needs_change':'نیازمند اصلاح بیشتر'}


@wellbeing.before_app_request
def limit_expert_legacy_access():
    user = get_current_user()
    if user and user.role == 'oh_expert' and request.blueprint == 'main':
        allowed = {'main.index', 'main.login', 'main.logout', 'main.preferences', 'main.notifications_page',
                   'main.read_notification', 'main.health', 'main.service_worker', 'main.manifest'}
        if request.endpoint not in allowed:
            abort(403)


def gate(*roles):
    def decorate(fn):
        @wraps(fn)
        @login_required
        def inner(*args, **kwargs):
            user = get_current_user()
            if user.role not in roles or (user.role == 'manager' and not user.is_appointed_manager):
                abort(403)
            return fn(*args, **kwargs)
        return inner
    return decorate


def section_ids():
    user = get_current_user()
    if user.role in ('manager', 'observer'):
        return [s.id for s in Section.query.all()]
    return [s.id for s in user.sections]


def scoped_section(sid):
    if sid not in section_ids():
        abort(403)
    section = db.session.get(Section, sid)
    if not section:
        abort(404)
    return section


def scoped_task(tid):
    task = db.session.get(WorkTask, tid)
    if not task:
        abort(404)
    scoped_section(task.section_id)
    return task


def worker(uid, sid):
    person = db.session.get(User, uid)
    if not person or person.role != 'staff' or not person.is_active or sid not in [s.id for s in person.sections]:
        abort(403)
    return person


def text(name, maxlen=2000, required=True):
    value = request.form.get(name, '').strip()
    if len(value) > maxlen or (required and not value):
        raise ValueError('فیلدهای لازم را تکمیل و متن را کوتاه‌تر کنید.')
    return value


def integer(name):
    value = number(request.form.get(name), 'شناسه', 1, 1000000000)
    if not value.is_integer():
        raise ValueError('شناسه معتبر نیست.')
    return int(value)


def due(name='review_due'):
    value = parse_day(request.form.get(name, ''))
    if value < iran_now().date():
        raise ValueError('موعد بازبینی جدید نباید در گذشته باشد.')
    return value


def audit(action):
    db.session.add(ActivityLog(user_id=get_current_user().id, action=action, details='ثبت در بخش سلامت و ایمنی؛ جزئیات خصوصی در لاگ درج نمی‌شود.'))


def notify(uid, target, title='پیگیری سلامت و ایمنی'):
    user = db.session.get(User, uid)
    if user and user.is_active:
        db.session.add(Notification(user_id=uid, title=title, body='یک مورد برای بررسی در کارتابل شما ثبت شده است.', target_url=target, notification_type='wellbeing'))


def render(page, **values):
    return render_template('wellbeing/' + page + '.html', status_labels=STATUS, outcomes=OUTCOMES,
                           date_label=date_label, today=iran_now().date(), **values)


def fail(exc):
    db.session.rollback()
    flash(str(exc) if isinstance(exc, ValueError) else 'ذخیره انجام نشد؛ دوباره تلاش کنید.', 'danger')


@wellbeing.route('/')
@gate('manager', 'observer', 'supervisor', 'staff', *EXPERTS)
def index():
    tasks = WorkTask.query.filter(WorkTask.section_id.in_(section_ids()), WorkTask.is_active.is_(True)).all()
    assessments = {t.id: TaskAssessment.query.filter_by(task_id=t.id).order_by(TaskAssessment.id.desc()).first() for t in tasks}
    return render('index', tasks=tasks, assessments=assessments)


@wellbeing.route('/tasks/<int:tid>', methods=['GET', 'POST'])
@gate('manager', 'observer', 'supervisor', 'staff', *EXPERTS)
def task(tid):
    item = scoped_task(tid)
    user = get_current_user()
    eligible_people = User.query.filter(User.role == 'staff', User.is_active.is_(True), User.sections.any(id=item.section_id)).order_by(User.full_name).all()
    selected_person = user if user.role == 'staff' else next((p for p in eligible_people if p.id == request.args.get('person_id', type=int)), None)
    if request.method == 'POST':
        if user.role not in EXPERTS:
            abort(403)
        try:
            conditions = parse_conditions(request.form)
            state = request.form.get('status')
            if state not in STATUS:
                raise ValueError('وضعیت معتبر نیست.')
            if state == 'reviewed' and (any(conditions[k] is None for k in MEASUREMENTS) or any(conditions[k] == 'unknown' for k in CONDITIONS)):
                raise ValueError('برای وضعیت بررسی‌شده، همه شرایط را تعیین کنید؛ اگر موردی مصداق ندارد صفر یا خیر و دلیل آن را ثبت کنید.')
            row = TaskAssessment(task_id=tid, section_id=item.section_id, task_title=item.title,
                                 conditions=conditions, status=state, controls=text('controls'),
                                 reference=text('reference', 500), reviewer_id=user.id,
                                 reviewer_name=user.full_name, review_due=due())
            db.session.add(row)
            audit('ارزیابی تخصصی وظیفه')
            db.session.commit()
            flash('ارزیابی ثبت شد؛ سابقه قبلی حفظ شد.', 'success')
            return redirect(url_for('wellbeing.task', tid=tid))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    history = TaskAssessment.query.filter_by(task_id=tid).order_by(TaskAssessment.id.desc()).all()
    latest = history[0] if history else None
    guidance = recommendations(SimpleNamespace(ergonomics=latest.conditions)) if latest else []
    operational_advice = None
    selected_tools = []
    if selected_person:
        operational_advice = FunctionalAdvice.query.filter_by(user_id=selected_person.id, section_id=item.section_id).order_by(FunctionalAdvice.id.desc()).first()
        spec = db.session.get(WorkSection, item.section_id)
        if operational_advice and latest:
            guidance.extend(recommendations(SimpleNamespace(ergonomics=latest.conditions), SimpleNamespace(
                accommodations=operational_advice.limits, review_due=operational_advice.review_due,
                restrictions=operational_advice.advice)))
        elif operational_advice and not latest:
            guidance.append(dict(title='مقایسه فردی', message='برای این وظیفه ارزیابی کارشناس ثبت نشده و تطبیق محدودیت فرد با شرایط واقعی ممکن نیست.', level='warning'))
        elif not operational_advice:
            guidance.append(dict(title='محدودیت کاری نامشخص', message='توصیه اجرایی مستند کارشناس بهداشت ثبت نشده است؛ این وضعیت به معنی نداشتن محدودیت نیست.', level='warning'))
        selected_tools = ToolFit.query.filter_by(task_id=tid, user_id=selected_person.id, is_active=True).order_by(ToolFit.id.desc()).all()
    return render('task', task=item, history=history, latest=latest, guidance=guidance,
                  measurements=MEASUREMENTS, conditions=CONDITIONS, expert=user.role in EXPERTS,
                  people=eligible_people, selected_person=selected_person,
                  operational_advice=operational_advice, selected_tools=selected_tools)


@wellbeing.route('/clinical', methods=['GET', 'POST'])
@gate('physician')
def clinical():
    user = get_current_user()
    ids = section_ids()
    people = User.query.filter(User.role == 'staff', User.is_active.is_(True), User.sections.any(Section.id.in_(ids))).all()
    if request.method == 'POST':
        try:
            sid, uid = integer('section_id'), integer('user_id')
            scoped_section(sid)
            person = worker(uid, sid)
            values = {key: number(request.form.get(key), label, 0, maximum, True) for key, (label, maximum) in LIMITS.items() if key != 'fitted_handle_cm'}
            advice, reference, review_due = text('advice'), text('reference', 300), due()
            private_note = text('private_note', 4000, False)
            if private_note:
                db.session.add(ClinicalNote(user_id=uid, section_id=sid, clinician_id=user.id, note=private_note))
            db.session.add(FunctionalAdvice(user_id=uid, section_id=sid, advice=advice, limits=values,
                                           reference=reference, review_due=review_due,
                                           clinician_id=user.id, clinician_name=user.full_name))
            profile = db.session.get(WorkProfile, uid)
            if profile is None:
                profile = WorkProfile(user_id=uid)
                db.session.add(profile)
            profile.accommodations = dict(profile.accommodations or {}, **values)
            profile.restrictions, profile.reference, profile.review_due = advice, reference, review_due
            notify(uid, '/wellbeing/advice')
            audit('ثبت توصیه اجرایی طب کار')
            db.session.commit()
            flash('نظر اجرایی و یادداشت خصوصی در دسترسی‌های جدا ذخیره شدند.', 'success')
            return redirect(url_for('wellbeing.clinical', user_id=uid))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    selected = next((p for p in people if p.id == request.args.get('user_id', type=int)), None)
    notes = ClinicalNote.query.filter_by(user_id=selected.id, section_id=ids[0]).order_by(ClinicalNote.id.desc()).all() if selected and len(ids) == 1 else (
        ClinicalNote.query.filter(ClinicalNote.user_id == selected.id, ClinicalNote.section_id.in_(ids)).order_by(ClinicalNote.id.desc()).all() if selected else [])
    advice_rows = FunctionalAdvice.query.filter_by(user_id=selected.id).order_by(FunctionalAdvice.id.desc()).all() if selected else []
    return render('clinical', people=people, sections=Section.query.filter(Section.id.in_(ids)).all(), selected=selected, notes=notes, advice_rows=advice_rows,
                  private_backup=latest_clinical_snapshot(current_app._get_current_object()))


@wellbeing.route('/clinical/backup')
@gate('physician')
def clinical_backup():
    snapshot = latest_clinical_snapshot(current_app._get_current_object())
    if not snapshot:
        flash('نسخه پشتیبان خصوصی هنوز ساخته نشده است.', 'warning')
        return redirect(url_for('wellbeing.clinical'))
    return send_file(snapshot, as_attachment=True, download_name=snapshot.name,
                     mimetype='application/octet-stream', max_age=0)


@wellbeing.route('/advice')
@gate('manager', 'staff', *EXPERTS)
def advice():
    user = get_current_user()
    query = FunctionalAdvice.query
    if user.role == 'staff':
        query = query.filter_by(user_id=user.id)
    elif user.role in EXPERTS:
        query = query.filter(FunctionalAdvice.section_id.in_(section_ids()))
    rows = query.order_by(FunctionalAdvice.id.desc()).all()
    names = {u.id:u.full_name for u in User.query.filter(User.id.in_([r.user_id for r in rows])).all()}
    return render('advice', rows=rows, names=names)


def tool_allowed(row):
    if not row:
        abort(404)
    scoped_section(row.section_id)
    if get_current_user().role == 'staff' and row.user_id != get_current_user().id:
        abort(403)


@wellbeing.route('/tools', methods=['GET', 'POST'])
@gate('manager', 'observer', 'supervisor', 'staff', *EXPERTS)
def tools_page():
    user = get_current_user()
    ids = section_ids()
    tasks = WorkTask.query.filter(WorkTask.section_id.in_(ids), WorkTask.is_active.is_(True)).all()
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            if action == 'retire':
                if user.role not in ('manager', *EXPERTS):
                    abort(403)
                row = db.session.get(ToolFit, integer('tool_id'))
                tool_allowed(row)
                row.is_active = False
            elif action == 'add':
                if user.role not in EXPERTS:
                    abort(403)
                task = scoped_task(integer('task_id'))
                person = worker(integer('user_id'), task.section_id)
                row = ToolFit(section_id=task.section_id, user_id=person.id, task_id=task.id,
                              name=text('name',150), asset_code=text('asset_code',100),
                              setting=text('setting',500), instruction=text('instruction'),
                              available=request.form.get('available') == '1',
                              fit_status=request.form.get('fit_status', ''), examiner_id=user.id,
                              examiner_name=user.full_name, review_due=due())
                if row.fit_status not in {'fits', 'does_not_fit', 'not_checked'}:
                    raise ValueError('نتیجه تطبیق ابزار را از فهرست انتخاب کنید.')
                photo = request.files.get('image')
                if photo and photo.filename:
                    try:
                        image = Image.open(photo.stream)
                        if image.width * image.height > 16000000:
                            raise ValueError('تصویر بیش از حد بزرگ است.')
                        image.load()
                        image = image.convert('RGB')
                        image.thumbnail((1200, 1200))
                        output = BytesIO()
                        image.save(output, format='JPEG', quality=85)
                        row.image = output.getvalue()
                    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                        raise ValueError('فقط تصویر سالم ابزار پذیرفته می‌شود.')
                db.session.add(row)
                notify(person.id, '/wellbeing/tools')
            else:
                raise ValueError('عملیات معتبر نیست.')
            audit('ثبت شناسنامه ابزار')
            db.session.commit()
            return redirect(url_for('wellbeing.tools_page'))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    query = ToolFit.query.filter(ToolFit.section_id.in_(ids))
    if user.role == 'staff':
        query = query.filter_by(user_id=user.id)
    people = User.query.filter(User.role == 'staff', User.is_active.is_(True), User.sections.any(Section.id.in_(ids))).all() if user.role != 'staff' else []
    return render('tools', rows=query.order_by(ToolFit.id.desc()).all(), tasks=tasks, people=people,
                  names={u.id:u.full_name for u in people}, expert=user.role in EXPERTS)


@wellbeing.route('/tools/<int:tool_id>/image')
@gate('manager', 'observer', 'supervisor', 'staff', *EXPERTS)
def tool_image(tool_id):
    row = db.session.get(ToolFit, tool_id)
    tool_allowed(row)
    if not row.image:
        abort(404)
    return send_file(BytesIO(row.image), mimetype='image/jpeg', max_age=0)


@wellbeing.route('/feedback', methods=['GET', 'POST'])
@gate('staff', 'oh_expert')
def feedback():
    user = get_current_user()
    ids = section_ids()
    tasks = (WorkTask.query.filter(WorkTask.section_id.in_(ids), WorkTask.is_active.is_(True)).all()
             if user.role == 'staff' and ids else [])
    if request.method == 'POST':
        if user.role != 'staff':
            abort(403)
        try:
            task = scoped_task(integer('task_id'))
            row = PrivateFeedback(user_id=user.id, section_id=task.section_id, task_id=task.id, body=text('body',4000))
            db.session.add(row)
            experts = User.query.filter(User.role == 'oh_expert', User.is_active.is_(True), User.sections.any(id=task.section_id)).all()
            for expert in experts:
                notify(expert.id, '/wellbeing/feedback', 'پیام خصوصی پرسنل خدمات')
            db.session.commit()
            flash('پیام خصوصی برای کارشناس بهداشت ثبت شد.' if experts else 'پیام ذخیره شد؛ هنوز کارشناس بهداشت فعالی برای بخش تعیین نشده است.', 'success' if experts else 'warning')
            return redirect(url_for('wellbeing.feedback'))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    query = PrivateFeedback.query
    query = query.filter_by(user_id=user.id) if user.role == 'staff' else query.filter(PrivateFeedback.section_id.in_(ids))
    return render('feedback', rows=query.order_by(PrivateFeedback.id.desc()).all(), tasks=tasks)


@wellbeing.route('/feedback/<int:fid>', methods=['GET', 'POST'])
@gate('staff', 'oh_expert')
def feedback_detail(fid):
    user = get_current_user()
    row = db.session.get(PrivateFeedback, fid)
    if not row:
        abort(404)
    if user.role == 'staff':
        if row.user_id != user.id:
            abort(403)
    else:
        scoped_section(row.section_id)
    if request.method == 'POST':
        try:
            body = text('body',4000)
            if user.role == 'oh_expert':
                status = request.form.get('status')
                if status not in ('new','following','closed'):
                    raise ValueError('وضعیت معتبر نیست.')
                row.status = status
                notify(row.user_id, f'/wellbeing/feedback/{fid}')
            else:
                row.status = 'following'
                experts = User.query.filter(User.role == 'oh_expert', User.is_active.is_(True), User.sections.any(id=row.section_id)).all()
                for expert in experts:
                    notify(expert.id, f'/wellbeing/feedback/{fid}', 'پاسخ جدید به پیام خصوصی')
            db.session.add(FeedbackReply(feedback_id=fid, author_id=user.id, author_name=user.full_name, body=body))
            db.session.commit()
            return redirect(url_for('wellbeing.feedback_detail', fid=fid))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    return render('feedback_detail', row=row, replies=FeedbackReply.query.filter_by(feedback_id=fid).order_by(FeedbackReply.id).all())


@wellbeing.route('/actions', methods=['GET', 'POST'])
@gate('manager', 'observer', 'supervisor', *EXPERTS)
def actions():
    user = get_current_user()
    ids = section_ids()
    tasks = WorkTask.query.filter(WorkTask.section_id.in_(ids), WorkTask.is_active.is_(True)).all()
    if request.method == 'POST':
        if user.role not in ('manager', *EXPERTS):
            abort(403)
        try:
            task = scoped_task(integer('task_id'))
            responsible = db.session.get(User, integer('responsible_id'))
            if not responsible or not responsible.is_active or responsible.role not in ('manager','supervisor',*EXPERTS):
                raise ValueError('مسئول پیگیری معتبر نیست.')
            if responsible.role == 'manager':
                if not responsible.is_appointed_manager:
                    raise ValueError('مسئول خدمات هنوز منصوب نشده است.')
            elif task.section_id not in [s.id for s in responsible.sections]:
                raise ValueError('مسئول پیگیری باید به بخش دسترسی داشته باشد.')
            row = SafetyAction(section_id=task.section_id, task_id=task.id, title=text('title',180),
                               before=text('before'), target=text('target'), responsible_id=responsible.id,
                               due_date=due('due_date'), created_by=user.id)
            db.session.add(row)
            notify(responsible.id, '/wellbeing/actions')
            audit('تعریف اقدام اصلاحی ایمنی')
            db.session.commit()
            return redirect(url_for('wellbeing.actions'))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    people = User.query.filter(User.is_active.is_(True), User.role.in_(('manager','supervisor',*EXPERTS))).all()
    people = [p for p in people if (p.role == 'manager' and p.is_appointed_manager) or any(s.id in ids for s in p.sections)]
    query = SafetyAction.query.filter(SafetyAction.section_id.in_(ids))
    if user.role not in ('manager', *EXPERTS):
        query = query.filter(SafetyAction.responsible_id == user.id)
    return render('actions', rows=query.order_by(SafetyAction.id.desc()).all(), tasks=tasks, people=people)


@wellbeing.route('/actions/<int:aid>', methods=['GET', 'POST'])
@gate('manager', 'observer', 'supervisor', *EXPERTS)
def action_detail(aid):
    row = db.session.get(SafetyAction, aid)
    if not row:
        abort(404)
    scoped_section(row.section_id)
    user = get_current_user()
    can_update = user.role in ('manager', *EXPERTS) or user.id == row.responsible_id
    if not can_update:
        abort(403)
    if request.method == 'POST':
        if not can_update or user.role == 'observer':
            abort(403)
        try:
            outcome = request.form.get('outcome')
            if outcome not in ('implemented','effective','needs_change'):
                raise ValueError('نتیجه معتبر نیست.')
            if outcome in ('effective','needs_change') and user.role not in EXPERTS:
                abort(403)
            result = text('result')
            next_due = due('due_date') if outcome == 'needs_change' else row.due_date
            db.session.add(SafetyReview(action_id=aid, author_id=user.id, author_name=user.full_name, outcome=outcome, result=result))
            row.status = outcome
            row.due_date = next_due
            row.due_notified = False
            audit('پیگیری نتیجه اقدام ایمنی')
            notify(row.responsible_id, f'/wellbeing/actions/{aid}')
            db.session.commit()
            return redirect(url_for('wellbeing.action_detail', aid=aid))
        except (ValueError, SQLAlchemyError) as exc:
            fail(exc)
    return render('action_detail', row=row, can_update=can_update,
                  history=SafetyReview.query.filter_by(action_id=aid).order_by(SafetyReview.id).all())


def notify_due_actions():
    """Called by the existing local reminder worker; no external scheduler needed."""
    rows = SafetyAction.query.filter(SafetyAction.due_date <= iran_now().date(), SafetyAction.status != 'effective', SafetyAction.due_notified.is_(False)).all()
    for row in rows:
        responsible = db.session.get(User, row.responsible_id)
        if not responsible or not responsible.is_active:
            continue
        notify(row.responsible_id, f'/wellbeing/actions/{row.id}', 'موعد بررسی اقدام اصلاحی')
        row.due_notified = True
