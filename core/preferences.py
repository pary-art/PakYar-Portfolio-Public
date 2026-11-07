"""Allowed personal appearance preferences for PakYar users."""

PREFERENCE_THEMES = {
    "ocean": {
        "label": "اقیانوسی",
        "description": "آرام، رسمی و مناسب استفادهٔ روزانه",
        "icon": "fa-water",
        "preview": ("#123b52", "#0f766e", "#14b8a6"),
    },
    "emerald": {
        "label": "زمردی",
        "description": "سبز و انرژی‌بخش با حس پاکیزگی",
        "icon": "fa-leaf",
        "preview": ("#064e3b", "#059669", "#10b981"),
    },
    "royal": {
        "label": "سلطنتی",
        "description": "آبی بنفش مدرن و جدی",
        "icon": "fa-gem",
        "preview": ("#312e81", "#4f46e5", "#818cf8"),
    },
    "sunset": {
        "label": "غروب",
        "description": "گرم، پررنگ و متفاوت",
        "icon": "fa-sun",
        "preview": ("#7c2d12", "#ea580c", "#f97316"),
    },
    "rose": {
        "label": "رز",
        "description": "نرم، شاد و چشم‌نواز",
        "icon": "fa-heart",
        "preview": ("#881337", "#e11d48", "#fb7185"),
    },
    "graphite": {
        "label": "گرافیتی",
        "description": "خنثی، مینیمال و حرفه‌ای",
        "icon": "fa-moon",
        "preview": ("#1f2937", "#475569", "#64748b"),
    },
}

FONT_SCALE_OPTIONS = {
    "small": {
        "label": "کوچک",
        "description": "اطلاعات بیشتر در صفحه",
        "sample": "Aa",
    },
    "normal": {
        "label": "استاندارد",
        "description": "اندازهٔ پیشنهادی",
        "sample": "Aa",
    },
    "large": {
        "label": "بزرگ",
        "description": "خوانایی بیشتر",
        "sample": "Aa",
    },
    "xlarge": {
        "label": "خیلی بزرگ",
        "description": "مناسب دید ضعیف‌تر",
        "sample": "Aa",
    },
}

DATE_MODE_OPTIONS = {
    "jalali": {
        "label": "شمسی",
        "description": "نمایش تاریخ با تقویم ایران",
        "sample": "۱۴۰۵/۰۶/۰۱",
    },
    "gregorian": {
        "label": "میلادی",
        "description": "نمایش تاریخ با تقویم میلادی",
        "sample": "۲۰۲۶/۰۸/۲۳",
    },
    "both": {
        "label": "هر دو",
        "description": "نمایش هم‌زمان شمسی و میلادی",
        "sample": "۱۴۰۵/۰۶/۰۱ | ۲۰۲۶/۰۸/۲۳",
    },
}

DEFAULT_PREFERENCES = {
    "theme": "ocean",
    "font_scale": "normal",
    "date_mode": "jalali",
    "compact_mode": False,
    "reduced_motion": False,
}
