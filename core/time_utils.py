from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    import jdatetime
except ImportError:  # pragma: no cover - installed from requirements.txt
    jdatetime = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None


UTC = timezone.utc
IRAN_TIMEZONE_NAME = "Asia/Tehran"

try:
    IRAN_TZ = ZoneInfo(IRAN_TIMEZONE_NAME) if ZoneInfo else None
except Exception:  # pragma: no cover - used when tzdata is not installed
    IRAN_TZ = None

if IRAN_TZ is None:
    # ایران از سال ۱۴۰۱ ساعت تابستانی ندارد؛ این مقدار برای اجرای آفلاین
    # روی ویندوزهایی که دادهٔ منطقه‌ای نصب نشده‌اند استفاده می‌شود.
    IRAN_TZ = timezone(timedelta(hours=3, minutes=30), name=IRAN_TIMEZONE_NAME)


JALALI_MONTH_NAMES = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ASCII_TO_PERSIAN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def utc_now():
    """Return a naive UTC datetime for storage in SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


def iran_now():
    """Return the current aware datetime in Iran time."""
    return datetime.now(UTC).astimezone(IRAN_TZ)


def normalize_digits(value):
    if not isinstance(value, str):
        return value
    return value.translate(_PERSIAN_DIGITS).translate(_ARABIC_DIGITS)


def parse_datetime(value):
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return value


def to_iran_datetime(value):
    """Convert a stored UTC-naive or aware datetime to Iran time."""
    value = parse_datetime(value)
    if not isinstance(value, datetime):
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(IRAN_TZ)


def iran_local_to_utc_naive(value):
    """Convert a local Iran datetime boundary to a naive UTC datetime."""
    if not isinstance(value, datetime):
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=IRAN_TZ)
    else:
        value = value.astimezone(IRAN_TZ)

    return value.astimezone(UTC).replace(tzinfo=None)


def gregorian_to_jalali(gy, gm, gd):
    if jdatetime is not None:
        converted = jdatetime.datetime.fromgregorian(
            datetime=datetime(gy, gm, gd)
        )
        return converted.year, converted.month, converted.day

    # Fallback for environments that have not installed requirements yet.
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + g_d_m[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def jalali_to_gregorian(jy, jm, jd):
    if jdatetime is None:
        raise RuntimeError(
            "برای تبدیل دقیق تقویم شمسی، وابستگی jdatetime را نصب کنید."
        )

    converted = jdatetime.date(jy, jm, jd).togregorian()
    return converted.year, converted.month, converted.day


def jalali_month_bounds(jy, jm):
    if not 1 <= jm <= 12:
        raise ValueError("Jalali month must be between 1 and 12.")

    start_year, start_month, start_day = jalali_to_gregorian(jy, jm, 1)

    if jm == 12:
        next_year, next_month = jy + 1, 1
    else:
        next_year, next_month = jy, jm + 1

    end_year, end_month, end_day = jalali_to_gregorian(
        next_year,
        next_month,
        1,
    )

    local_start = datetime(start_year, start_month, start_day)
    local_end = datetime(end_year, end_month, end_day)
    return iran_local_to_utc_naive(local_start), iran_local_to_utc_naive(local_end)


def current_jalali_month():
    now = iran_now()
    jy, jm, _ = gregorian_to_jalali(now.year, now.month, now.day)
    return jy, jm


def jalali_month_value(jy, jm):
    return f"{jy:04d}-{jm:02d}"


def jalali_month_label(jy, jm):
    return f"{JALALI_MONTH_NAMES[jm - 1]} {jy:04d}"


def parse_jalali_month(value):
    if not isinstance(value, str):
        return None

    normalized = normalize_digits(value.strip()).replace("/", "-")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", normalized)
    if not match:
        return None

    jy, jm = int(match.group(1)), int(match.group(2))
    if not 1200 <= jy <= 1600 or not 1 <= jm <= 12:
        return None

    return jy, jm


def format_jalali(value, format_str="%Y/%m/%d %H:%M", persian_digits=True):
    if not value:
        return "-"

    try:
        iran_value = to_iran_datetime(value)
        if iran_value is None:
            return str(value)

        jy, jm, jd = gregorian_to_jalali(
            iran_value.year,
            iran_value.month,
            iran_value.day,
        )
        result = (
            format_str.replace("%Y", f"{jy:04d}")
            .replace("%m", f"{jm:02d}")
            .replace("%d", f"{jd:02d}")
            .replace("%H", f"{iran_value.hour:02d}")
            .replace("%M", f"{iran_value.minute:02d}")
            .replace("%S", f"{iran_value.second:02d}")
        )
        return result.translate(_ASCII_TO_PERSIAN) if persian_digits else result
    except Exception:
        return str(value)


def format_gregorian(value, format_str="%Y/%m/%d %H:%M", persian_digits=True):
    """Format a stored timestamp in Iran time using the Gregorian calendar."""
    if not value:
        return "-"

    try:
        iran_value = to_iran_datetime(value)
        if iran_value is None:
            return str(value)

        result = (
            format_str.replace("%Y", f"{iran_value.year:04d}")
            .replace("%m", f"{iran_value.month:02d}")
            .replace("%d", f"{iran_value.day:02d}")
            .replace("%H", f"{iran_value.hour:02d}")
            .replace("%M", f"{iran_value.minute:02d}")
            .replace("%S", f"{iran_value.second:02d}")
        )
        return result.translate(_ASCII_TO_PERSIAN) if persian_digits else result
    except Exception:
        return str(value)
