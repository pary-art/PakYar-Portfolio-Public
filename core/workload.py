"""Transparent time estimates, not medical fitness or statutory hardship scores."""
import math
from datetime import date

from core.time_utils import jdatetime, normalize_digits

UNITS = {'area': 'مترمربع قابل نظافت', 'beds': 'تخت', 'occupied_beds': 'تخت اشغال‌شده',
         'toilets': 'سرویس بهداشتی', 'bins': 'سطل زباله', 'isolation_rooms': 'اتاق ایزوله',
         'fixed': 'تعداد دستی (کار، وسیله یا نوبت)'}
BASIS = {'estimate': 'برآورد محلی؛ تأییدنشده', 'measured': 'زمان‌سنجی محلی',
         'documented': 'مستند تأیید محلی ثبت شده'}
WEEKDAYS = [(5, 'شنبه'), (6, 'یکشنبه'), (0, 'دوشنبه'), (1, 'سه‌شنبه'),
            (2, 'چهارشنبه'), (3, 'پنجشنبه'), (4, 'جمعه')]
SHIFTS = ('صبح', 'عصر', 'شب')


def number(value, label, low=0, high=1000000, optional=False):
    value = normalize_digits(str(value or '')).strip().replace('٫', '.')
    if not value and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}: عدد معتبر وارد کنید.')
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f'{label}: مقدار باید بین {low} و {high} باشد.')
    return result


def parse_day(value):
    try:
        parts = normalize_digits(value).strip().replace('/', '-').split('-')
        year, month, day = (int(p) for p in parts)
        result = jdatetime.date(year, month, day).togregorian() if year < 1700 else date(year, month, day)
        if not 2000 <= result.year <= 2100:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError('تاریخ معتبر به شکل ۱۴۰۵/۰۸/۱۲ یا 2026-11-03 وارد کنید.')


def date_label(day):
    return jdatetime.date.fromgregorian(date=day).strftime('%Y/%m/%d')


def estimate(spec, tasks, day, shift):
    if spec is None:
        raise ValueError('ابتدا مشخصات بخش را تکمیل کنید.')
    rows = []
    for task in tasks:
        if not task.is_active or task.shift not in ('all', shift):
            continue
        if task.weekday is not None and task.weekday != day.weekday():
            continue
        quantity = task.quantity if task.unit == 'fixed' else getattr(spec, task.unit)
        rows.append(dict(title=task.title, unit=UNITS[task.unit], quantity=quantity,
                         repeats=task.repeats, minutes=task.minutes,
                         total=round(quantity * task.repeats * task.minutes, 4),
                         basis=task.basis, reference=task.reference,
                         approved_by=task.approved_by,
                         approved_date=task.approved_date.isoformat() if task.approved_date else '',
                         needs_review=task.needs_review))
    if not rows:
        raise ValueError('برای این روز و شیفت هنوز فعالیت زمان‌دار تعریف نشده است.')
    return {'rows': rows, 'total': round(sum(row['total'] for row in rows), 4),
            'basis': 'documented' if all(row['basis'] == 'documented' for row in rows) else 'estimate',
            'method': 'local-time-estimate-v1',
            'section': {field: getattr(spec, field) for field in UNITS if field != 'fixed'}}


def load_percent(minutes, available):
    return round(100 * minutes / available, 1) if available > 0 else None


def balanced_minutes(total, capacities, prior):
    """Water filling across selected staff, accounting for other sections.

    A time-only proposal, never a fitness decision or automatic reassignment.
    """
    if not capacities:
        return {}
    low = 0.0
    high = max((prior.get(uid, 0) + total) / cap for uid, cap in capacities.items()) + 1
    for _ in range(70):
        level = (low + high) / 2
        allocated = sum(max(0, level * cap - prior.get(uid, 0)) for uid, cap in capacities.items())
        if allocated < total:
            low = level
        else:
            high = level
    return {uid: max(0, high * cap - prior.get(uid, 0)) for uid, cap in capacities.items()}
