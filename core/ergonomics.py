"""Task-design guidance and explicit clinician-limit comparisons, not fitness scoring."""
from datetime import date
from core.time_utils import iran_now, jdatetime, normalize_digits
from core.workload import number

MEASUREMENTS = {
    'load_kg': ('بیشترین وزن بار جابه‌جاشونده (کیلوگرم)', 1000),
    'lifts_per_hour': ('دفعات بلندکردن بار در ساعت', 1000),
    'carry_m': ('مسافت حمل دستی در هر نوبت (متر)', 10000),
    'standing_minutes': ('بیشترین ایستادن پیوسته (دقیقه)', 1440),
    'handle_min_cm': ('کمترین طول دسته ابزار موجود (سانتی‌متر)', 500),
    'handle_max_cm': ('بیشترین طول دسته ابزار موجود (سانتی‌متر)', 500),
    'surface_min_cm': ('کمترین ارتفاع سطح کار (سانتی‌متر)', 500),
    'surface_max_cm': ('بیشترین ارتفاع سطح کار (سانتی‌متر)', 500),
}
CONDITIONS = {
    'adjustable': 'دسته ابزار قابل تنظیم است',
    'cart': 'چرخ‌دستی مناسب در دسترس است',
    'bending': 'کار شامل خم‌شدن یا چرخش مکرر تنه است',
    'overhead': 'کار بالاتر از شانه انجام می‌شود',
    'repetition': 'حرکات دست و مچ تکراری است',
    'stairs': 'مسیر حمل شامل پله است',
    'wet_floor': 'کف خیس یا لغزنده وجود دارد',
    'chemicals': 'تماس با شوینده یا ضدعفونی‌کننده وجود دارد',
    'isolation': 'مواجهه با پسماند عفونی یا محیط ایزوله وجود دارد',
}
LIMITS = {
    'lift_limit_kg': ('حد حمل بار طبق نظر طب کار (کیلوگرم)', 1000),
    'standing_limit_minutes': ('حد ایستادن پیوسته طبق نظر طب کار (دقیقه)', 1440),
    'fitted_handle_cm': ('طول دسته مناسب پس از تنظیم و ارزیابی عملی (سانتی‌متر)', 500),
}


def birth_date(value):
    if not value.strip():
        return None
    try:
        y, m, d = map(int, normalize_digits(value).replace('/', '-').split('-'))
        result = jdatetime.date(y, m, d).togregorian() if y < 1700 else date(y, m, d)
        if not date(1900, 1, 1) <= result <= iran_now().date():
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError('تاریخ تولد معتبر شمسی وارد کنید؛ تاریخ آینده پذیرفته نیست.')


def age_years(born, today=None):
    if born is None:
        return None
    # Birthdays follow the displayed Jalali calendar, including leap-day rollover.
    today = jdatetime.date.fromgregorian(date=today or iran_now().date())
    born = jdatetime.date.fromgregorian(date=born)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def parse_conditions(form):
    values = {key: number(form.get(key), label, 0, maximum, True)
              for key, (label, maximum) in MEASUREMENTS.items()}
    for key in CONDITIONS:
        value = form.get(key, 'unknown')
        if value not in ('yes', 'no', 'unknown'):
            raise ValueError('وضعیت شرایط کار معتبر نیست.')
        values[key] = value
    for start, end in [('handle_min_cm', 'handle_max_cm'), ('surface_min_cm', 'surface_max_cm')]:
        if (values[start] is None) != (values[end] is None):
            raise ValueError('حداقل و حداکثر ارتفاع را با هم تکمیل کنید.')
        if values[start] is not None and values[start] > values[end]:
            raise ValueError('حداقل ارتفاع نمی‌تواند بیشتر از حداکثر باشد.')
    return values


def recommendations(spec, profile=None):
    data = spec.ergonomics or {} if spec else {}
    rows = []
    def add(title, message, level='info'):
        rows.append(dict(title=title, message=message, level=level))
    missing = sum(data.get(key) is None for key in MEASUREMENTS) + sum(data.get(key, 'unknown') == 'unknown' for key in CONDITIONS)
    if missing:
        add('اطلاعات ناقص', f'{missing} مورد هنوز نامشخص است؛ نبود اطلاعات به معنی نبود خطر نیست.', 'warning')
    add('تناسب ابزار با فرد', 'طول دسته تی و ابزار را در اجرای واقعی با وضعیت راحت بدن تنظیم و ارزیابی کنید؛ طول مناسب ثبت‌شده توسط ارزیاب ملاک مقایسه است، نه نسبت حدسی قد به دسته.')
    if data.get('adjustable') == 'no':
        add('ابزار غیرقابل تنظیم', 'برای استفاده افراد با ابعاد بدنی متفاوت، ابزار سبک با دسته قابل تنظیم بررسی شود.', 'warning')
    if (data.get('load_kg') or 0) > 0:
        add('بلندکردن و حمل بار', 'وزن، دفعات، مسافت، ارتفاع دست، چرخش تنه و کیفیت گرفتن بار باید در ارزیابی تخصصی لحاظ شوند. این فرم وزن ایمن عمومی تعیین نمی‌کند.', 'warning')
        if data.get('cart') == 'no':
            add('وسیله حمل', 'امکان استفاده از وسیله حمل مناسب، کاهش اندازه بار و اصلاح مسیر بررسی شود.', 'warning')
    for key, title, message in [
        ('bending', 'خم‌شدن و چرخش تنه', 'ارتفاع کار، محل قرارگیری وسایل و ابزار دسته‌بلند بررسی و خم‌شدن تکراری کاهش یابد.'),
        ('overhead', 'کار بالاتر از شانه', 'روش یا ابزار دسترسی اصلاح شود تا کار طولانی با دست بالا کاهش یابد.'),
        ('repetition', 'حرکات تکراری', 'تنوع وظایف، ابزار مناسب و فرصت بازیابی عضلات در برنامه کاری بررسی شود.'),
        ('stairs', 'پله در مسیر حمل', 'مسیر جایگزین و روش حمل مناسب ارزیابی شود؛ صرف وجود چرخ‌دستی کافی نیست.'),
        ('wet_floor', 'لغزش', 'خشک‌کردن مسیر، علامت هشدار و کفش مناسب در روش کار لحاظ شود.'),
        ('chemicals', 'شوینده و ضدعفونی‌کننده', 'برگه اطلاعات ایمنی ماده، تهویه، آموزش رقیق‌سازی و حفاظت فردی مناسب بررسی شود.'),
        ('isolation', 'محیط عفونی', 'روش مصوب کنترل عفونت، تفکیک پسماند، آموزش و حفاظت فردی لازم بررسی شود.'),
    ]:
        if data.get(key) == 'yes':
            add(title, message, 'warning')
    if profile:
        limits = profile.accommodations or {}
        expired = profile.review_due and profile.review_due < iran_now().date()
        if expired:
            add('بازبینی نظر تخصصی', 'موعد بازبینی گذشته است؛ محدودیت‌های ثبت‌شده نیازمند بازبینی‌اند و خودکار لغو نمی‌شوند.', 'warning')
        for demand, limit, label in [('load_kg', 'lift_limit_kg', 'حمل بار'), ('standing_minutes', 'standing_limit_minutes', 'ایستادن پیوسته')]:
            if limits.get(limit) is not None:
                if data.get(demand) is None:
                    add(label, 'برای مقایسه با محدودیت مستند فرد، مقدار تقاضای کار را تکمیل کنید.', 'warning')
                elif data[demand] > limits[limit]:
                    add(label, f'تقاضای ثبت‌شده ({data[demand]}) از حد مستند فرد ({limits[limit]}) بیشتر است؛ پیش از واگذاری، کار اصلاح و نظر تخصصی بررسی شود.', 'danger')
                else:
                    add(label, 'در این مقایسه عددی تجاوزی از حد ثبت‌شده دیده نشد؛ این نتیجه تأیید تناسب کامل کار نیست.')
        fitted = limits.get('fitted_handle_cm')
        if fitted is not None:
            lo, hi = data.get('handle_min_cm'), data.get('handle_max_cm')
            if lo is None or hi is None:
                add('دسته ابزار فرد', 'دامنه طول ابزار موجود هنوز ثبت نشده است.', 'warning')
            elif not lo <= fitted <= hi:
                add('دسته ابزار فرد', f'طول ارزیابی‌شده برای فرد ({fitted} سانتی‌متر) خارج از دامنه ابزار موجود ({lo} تا {hi}) است؛ ابزار مناسب تهیه یا تنظیم شود.', 'danger')
            else:
                add('دسته ابزار فرد', 'طول ارزیابی‌شده داخل دامنه ثبت‌شده است؛ تنظیم واقعی، موجودبودن ابزار و وضعیت بدن بررسی شود.')
        if profile.restrictions:
            add('محدودیت متنی طب کار', profile.restrictions + ' — این متن باید توسط مسئول بررسی شود؛ سامانه آن را تفسیر پزشکی نمی‌کند.', 'warning')
    return rows
