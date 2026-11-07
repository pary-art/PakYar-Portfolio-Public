"""Role-scoped planning screens. Observer remains read-only."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError

from core.models import ActivityLog, Section, User, db
from core.routes import (enforce_session_timeout, get_current_user, login_required,
                         protect_state_changing_requests)
from core.time_utils import iran_now
from core.ergonomics import (MEASUREMENTS, CONDITIONS, LIMITS, birth_date, age_years,
                            parse_conditions, recommendations)
from core.workload import (BASIS, SHIFTS, UNITS, WEEKDAYS, balanced_minutes,
                           date_label, estimate, load_percent, number, parse_day)
from core.workload_models import (WorkAllocation, WorkCapacity, WorkPlan, WorkProfile,
                                  WorkSection, WorkTask)

workload = Blueprint('workload', __name__, url_prefix='/workload')
workload.before_request(enforce_session_timeout)
workload.before_request(protect_state_changing_requests)


def access(write=False):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user.role not in ('manager', 'observer', 'supervisor'):
                abort(403)
            if user.role == 'manager' and not user.is_appointed_manager:
                abort(403)
            if (write or request.method == 'POST') and user.role != 'manager':
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def profile_access():
    """Allow the occupational-health expert only into assigned staff profiles."""
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user.role not in ('manager', 'observer', 'supervisor', 'oh_expert'):
                abort(403)
            if user.role == 'manager' and not user.is_appointed_manager:
                abort(403)
            if request.method == 'POST' and user.role not in ('manager', 'oh_expert'):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def visible_sections():
    user = get_current_user()
    return Section.query.order_by(Section.name).all() if user.role in ('manager', 'observer') else sorted(user.sections, key=lambda s: s.name)


def section_for(section_id):
    section = db.session.get(Section, section_id)
    if not section:
        abort(404)
    if section.id not in {s.id for s in visible_sections()}:
        abort(403)
    return section


def text_field(name, limit, required=False):
    value = request.form.get(name, '').strip()
    if (required and not value) or len(value) > limit:
        raise ValueError('متن الزامی را تکمیل کنید و طول نوشته را کوتاه‌تر نگه دارید.')
    return value


def audit(action, details):
    # Keep physical measurements and functional restrictions out of general logs.
    db.session.add(ActivityLog(user_id=get_current_user().id, action=action, details=details))


def commit():
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def selection(values):
    day = parse_day(values.get('date', '') or date_label(iran_now().date()))
    shift = values.get('shift', SHIFTS[0])
    if shift not in SHIFTS:
        raise ValueError('شیفت معتبر انتخاب کنید.')
    return day, shift


def common():
    return dict(units=UNITS, basis_labels=BASIS, weekdays=WEEKDAYS, shifts=SHIFTS,
                ergo_measurements=MEASUREMENTS, ergo_conditions=CONDITIONS, ergo_limits=LIMITS,
                date_label=date_label, can_edit=get_current_user().role == 'manager')


@workload.route('/')
@access()
def index():
    error = None
    try:
        day, shift = selection(request.args)
    except ValueError as exc:
        error = str(exc)
        day, shift = iran_now().date(), SHIFTS[0]
    sections = visible_sections()
    ids = [s.id for s in sections]
    plans = WorkPlan.query.filter(WorkPlan.section_id.in_(ids), WorkPlan.work_date == day, WorkPlan.shift == shift).all()
    summaries = []
    for section in sections:
        plan = next((p for p in plans if p.section_id == section.id), None)
        summaries.append(dict(section=section, plan=plan,
                              assigned=sum(a.minutes for a in plan.allocations) if plan else 0))
    # Supervisors get totals ONLY from their sections, explicitly labelled as such.
    person = {}
    for plan in plans:
        for allocation in plan.allocations:
            if allocation.user_id is None:
                continue
            row = person.setdefault(allocation.user_id, dict(name=allocation.user_name, minutes=0, sections=[]))
            row['minutes'] += allocation.minutes
            row['sections'].append(plan.section.name)
    for uid, row in person.items():
        cap = WorkCapacity.query.filter_by(user_id=uid, work_date=day, shift=shift).first()
        row['available'] = cap.available if cap else 0
        row['percent'] = load_percent(row['minutes'], row['available'])
    return render_template('workload/index.html', summaries=summaries, people=person.values(),
                           day=day, shift=shift, error=error, **common())


@workload.route('/people')
@login_required
def expert_people():
    user = get_current_user()
    if user.role != 'oh_expert':
        abort(403)
    section_ids = [section.id for section in user.sections]
    people = (User.query.filter(User.role == 'staff', User.is_active.is_(True),
                                User.sections.any(Section.id.in_(section_ids)))
              .order_by(User.full_name).all())
    return render_template('workload/expert_people.html', people=people)


@workload.route('/people/<int:user_id>', methods=['GET', 'POST'])
@profile_access()
def profile(user_id):
    user = db.session.get(User, user_id)
    if not user or user.role != 'staff':
        abort(404)
    viewer = get_current_user()
    if viewer.role == 'oh_expert' and not user.is_active:
        abort(404)
    if viewer.role not in ('manager', 'observer') and not ({s.id for s in user.sections} & {s.id for s in viewer.sections}):
        abort(403)
    profile = db.session.get(WorkProfile, user_id)
    error = None
    if request.method == 'POST':
        try:
            values = dict(height_cm=number(request.form.get('height_cm'), 'قد', 50, 250, True),
                          weight_kg=number(request.form.get('weight_kg'), 'وزن', 20, 400, True),
                          restrictions=text_field('restrictions', 1000), reference=text_field('reference', 300),
                          review_due=parse_day(request.form['review_due']) if request.form.get('review_due') else None)
            values['birth_date'] = birth_date(request.form.get('birth_date', ''))
            has_physical_data = any(values[key] is not None for key in ('height_cm', 'weight_kg', 'birth_date'))
            consent_confirmed = request.form.get('privacy_consent') == '1'
            if has_physical_data and not consent_confirmed and not (profile and profile.consented_at):
                raise ValueError('برای ثبت قد، وزن یا تاریخ تولد، رضایت روشن فرد را بگیرید و تأیید کنید.')
            values['accommodations'] = {
                key: number(request.form.get(key), label, 0, maximum, True)
                for key, (label, maximum) in LIMITS.items()
            }
            if any(v is not None for v in values['accommodations'].values()) and not values['reference']:
                raise ValueError('برای حدود فردی یا تنظیم ابزار، نام ارزیاب و مرجع نظر تخصصی لازم است.')
            if values['restrictions'] and not values['reference']:
                raise ValueError('برای ثبت محدودیت کاری، مرجع ارزیابی یا توصیه را وارد کنید.')
            if not profile:
                profile = WorkProfile(user_id=user_id)
                db.session.add(profile)
            if consent_confirmed and has_physical_data:
                profile.consented_at = utc_now()
                profile.consent_recorded_by = viewer.id
            for key, value in values.items():
                setattr(profile, key, value)
            audit('ویرایش مشخصات کاری', f'شناسه پرسنل: {user_id}؛ بدون ثبت جزئیات خصوصی در لاگ')
            commit()
            flash('مشخصات ذخیره شد؛ قد و وزن ضریب بار کاری نیستند.', 'success')
            return redirect(url_for('workload.profile', user_id=user_id))
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            error = str(exc) if isinstance(exc, ValueError) else 'ذخیره انجام نشد؛ صفحه را تازه و دوباره تلاش کنید.'
    return render_template('workload/profile.html', person=user, profile=profile, error=error,
                           age=age_years(profile.birth_date) if profile else None,
                           can_edit_profile=viewer.role in ('manager', 'oh_expert'), **common())


@workload.route('/sections/<int:section_id>/setup', methods=['GET', 'POST'])
@access()
def setup(section_id):
    section = section_for(section_id)
    spec = db.session.get(WorkSection, section_id)
    error = None
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            if action == 'spec':
                values = {key: number(request.form.get(key), label) for key, label in UNITS.items() if key != 'fixed'}
                for key in values:
                    if key != 'area' and not values[key].is_integer():
                        raise ValueError('تعدادها باید عدد صحیح باشند.')
                if values['occupied_beds'] > values['beds']:
                    raise ValueError('تخت اشغال‌شده نمی‌تواند بیشتر از کل تخت‌ها باشد.')
                equipment = text_field('equipment', 1000)
                ergonomic_values = parse_conditions(request.form)
                if not spec:
                    spec = WorkSection(section_id=section_id)
                    db.session.add(spec)
                for key, value in values.items():
                    setattr(spec, key, value)
                spec.equipment = equipment
                spec.ergonomics = ergonomic_values
            elif action == 'task':
                unit = request.form.get('unit')
                basis = request.form.get('basis')
                shift = request.form.get('shift')
                if unit not in UNITS or basis not in BASIS or shift not in ('all', *SHIFTS):
                    raise ValueError('نوع فعالیت، مبنا یا شیفت معتبر نیست.')
                weekday = request.form.get('weekday', '')
                if weekday and weekday not in {str(key) for key, _ in WEEKDAYS}:
                    raise ValueError('روز هفته معتبر نیست.')
                weekday = int(weekday) if weekday != '' else None
                reference = text_field('reference', 500, True)
                approver = text_field('approved_by', 150)
                approved_date = parse_day(request.form['approved_date']) if request.form.get('approved_date') else None
                if basis == 'documented' and (not approver or not approved_date):
                    raise ValueError('ثبت تأیید محلی به نام تأییدکننده، تاریخ و مرجع سند نیاز دارد.')
                task = WorkTask(section_id=section_id, title=text_field('title', 150, True),
                                unit=unit, quantity=number(request.form.get('quantity', '1'), 'تعداد دستی', 0, 100000),
                                repeats=number(request.form.get('repeats'), 'دفعات', 0.01, 1000),
                                minutes=number(request.form.get('minutes'), 'دقیقه هر واحد', 0.001, 1440),
                                shift=shift, weekday=weekday, basis=basis, reference=reference,
                                approved_by=approver, approved_date=approved_date,
                                needs_review=request.form.get('needs_review') == '1')
                db.session.add(task)
            elif action == 'deactivate':
                task = db.session.get(WorkTask, int(number(request.form.get('task_id'), 'شناسه', 1)))
                if not task or task.section_id != section_id:
                    abort(404)
                task.is_active = False
            else:
                raise ValueError('عملیات معتبر نیست.')
            audit('تنظیم بار کاری بخش', f'بخش {section_id}؛ عملیات {action}')
            commit()
            flash('ذخیره شد. ارزیابی‌های قبلی بدون تغییر باقی می‌مانند.', 'success')
            return redirect(url_for('workload.setup', section_id=section_id))
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            error = str(exc) if isinstance(exc, ValueError) else 'ذخیره انجام نشد؛ صفحه را تازه و دوباره تلاش کنید.'
    tasks = WorkTask.query.filter_by(section_id=section_id, is_active=True).order_by(WorkTask.id).all()
    selected_person = None
    profile_data = None
    staff = []
    if get_current_user().role == 'manager':
        staff = User.query.filter(User.role == 'staff', User.is_active.is_(True), User.sections.any(id=section_id)).order_by(User.full_name).all()
        selected_person = next((u for u in staff if u.id == request.args.get('person_id', type=int)), None)
        if selected_person:
            profile_data = db.session.get(WorkProfile, selected_person.id)
    return render_template('workload/setup.html', section=section, spec=spec, tasks=tasks, error=error,
                           guidance=recommendations(spec, profile_data), staff=staff,
                           selected_person=selected_person, **common())


def prior_load(user_id, day, shift, exclude_plan=None):
    query = db.session.query(func.coalesce(func.sum(WorkAllocation.minutes), 0)).join(WorkPlan).filter(
        WorkAllocation.user_id == user_id, WorkPlan.work_date == day, WorkPlan.shift == shift)
    if exclude_plan:
        query = query.filter(WorkPlan.id != exclude_plan)
    return float(query.scalar())


@workload.route('/sections/<int:section_id>/plan', methods=['GET', 'POST'])
@access()
def plan(section_id):
    section = section_for(section_id)
    try:
        day, shift = selection(request.values)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('workload.index'))
    plan = WorkPlan.query.filter_by(section_id=section_id, work_date=day, shift=shift).first()
    staff = User.query.filter(User.role == 'staff', User.is_active.is_(True), User.sections.any(id=section_id)).order_by(User.full_name).all()
    error, preview, proposal = None, None, None
    try:
        preview = estimate(db.session.get(WorkSection, section_id), WorkTask.query.filter_by(section_id=section_id).all(), day, shift)
    except ValueError as exc:
        error = str(exc)
    capacities = {u.id: WorkCapacity.query.filter_by(user_id=u.id, work_date=day, shift=shift).first() for u in staff}
    # Global prior load is only disclosed to the global planner, never another section's supervisor.
    prior = {u.id: prior_load(u.id, day, shift, plan.id if plan else None) for u in staff} if get_current_user().role == 'manager' else {}
    if request.method == 'POST':
        try:
            if not preview:
                raise ValueError(error or 'اطلاعات کافی نیست.')
            posted_revision = int(number(request.form.get('revision'), 'نسخه فرم', 0))
            if posted_revision != (plan.revision if plan else 0):
                raise ValueError('این برنامه در پنجره دیگری تغییر کرده؛ صفحه را دوباره باز کنید.')
            selected = set(request.form.getlist('worker'))
            eligible = {str(u.id): u for u in staff}
            if not selected or not selected <= set(eligible):
                raise ValueError('حداقل یک پرسنل فعال از همین بخش انتخاب کنید.')
            parsed = {}
            for uid_text in sorted(selected):
                u = eligible[uid_text]
                length = number(request.form.get(f'length_{u.id}'), 'طول شیفت (دقیقه)', 1, 1440)
                rest = number(request.form.get(f'rest_{u.id}'), 'استراحت', 0, 1440)
                other = number(request.form.get(f'other_{u.id}'), 'سایر وظایف', 0, 1440)
                if length - rest - other <= 0:
                    raise ValueError(f'زمان مفید {u.full_name} باید بیشتر از صفر باشد.')
                pct = number(request.form.get(f'percent_{u.id}', '0'), 'درصد سهم از کار بخش', 0, 100)
                parsed[u.id] = dict(user=u, length=length, rest=rest, other=other, available=length-rest-other, percent=pct)
            action = request.form.get('action')
            if action == 'suggest':
                minutes = balanced_minutes(preview['total'], {uid: row['available'] for uid, row in parsed.items()}, prior)
                proposal = [dict(name=row['user'].full_name, minutes=minutes[uid],
                                 percent=(minutes[uid] / preview['total'] * 100 if preview['total'] else 0),
                                 load=load_percent(prior[uid] + minutes[uid], row['available'])) for uid, row in parsed.items()]
                if preview['total'] > 0:
                    # Make the displayed two-decimal shares sum to exactly 100.
                    for row in proposal:
                        row['percent'] = round(row['percent'], 2)
                    largest = max(proposal, key=lambda row: row['percent'])
                    largest['percent'] = round(largest['percent'] + 100 - sum(row['percent'] for row in proposal), 2)
            elif action == 'save':
                if sum(row['percent'] for row in parsed.values()) > 100.000001:
                    raise ValueError('جمع سهم افراد نمی‌تواند بیشتر از ۱۰۰ درصد باشد.')
                if request.form.get('reviewed') != '1':
                    raise ValueError('بررسی محدودیت‌های کاری و آموزش افراد را تأیید کنید.')
                if not plan:
                    plan = WorkPlan(section_id=section_id, work_date=day, shift=shift, revision=1)
                    db.session.add(plan)
                else:
                    result = db.session.execute(update(WorkPlan).where(
                        WorkPlan.id == plan.id, WorkPlan.revision == posted_revision
                    ).values(revision=posted_revision + 1).execution_options(synchronize_session=False))
                    if result.rowcount != 1:
                        raise ValueError('هم‌زمان برنامه تغییر کرده؛ صفحه را دوباره باز کنید.')
                    plan.revision = posted_revision + 1
                    plan.allocations.clear()
                    db.session.flush()
                plan.total_minutes = preview['total']
                plan.snapshot = preview
                plan.created_by = get_current_user().id
                for uid, row in parsed.items():
                    cap = capacities[uid]
                    if not cap:
                        cap = WorkCapacity(user_id=uid, work_date=day, shift=shift)
                        db.session.add(cap)
                    cap.shift_minutes, cap.break_minutes, cap.other_minutes = row['length'], row['rest'], row['other']
                    plan.allocations.append(WorkAllocation(user_id=uid, user_name=row['user'].full_name,
                                                           percent=row['percent'], minutes=preview['total'] * row['percent'] / 100))
                audit('ثبت تقسیم بار کاری', f'بخش {section_id}؛ {day.isoformat()}؛ شیفت {shift}؛ نسخه {plan.revision}')
                commit()
                flash('ارزیابی و سهم افراد ذخیره شد.', 'success')
                return redirect(url_for('workload.plan', section_id=section_id, date=date_label(day), shift=shift))
            else:
                raise ValueError('عملیات معتبر نیست.')
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            error = str(exc) if isinstance(exc, ValueError) else 'ذخیره انجام نشد؛ ممکن است هم‌زمان برنامه تغییر کرده باشد. صفحه را تازه کنید.'
    # Only the responsible services manager receives private profile details.
    profiles = {u.id: db.session.get(WorkProfile, u.id) for u in staff} if get_current_user().role == 'manager' else {}
    allocations = {a.user_id: a for a in plan.allocations} if plan else {}
    return render_template('workload/plan.html', section=section, day=day, shift=shift, plan=plan,
                           preview=preview, staff=staff, capacities=capacities, prior=prior,
                           profiles=profiles, allocations=allocations, proposal=proposal, error=error, **common())
