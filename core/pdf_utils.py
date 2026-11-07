"""ابزارهای خروجی PDF فارسی و راست‌به‌چپ."""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import XPreformatted


_RESHAPER = arabic_reshaper.ArabicReshaper(
    {
        "delete_harakat": False,
        "support_ligatures": True,
    }
)
PERSIAN_DIGITS = str.maketrans(
    "0123456789,",
    "۰۱۲۳۴۵۶۷۸۹٬",
)


def register_persian_fonts(fonts_dir=None):
    if "Vazirmatn" in pdfmetrics.getRegisteredFontNames():
        return "Vazirmatn", "Vazirmatn-Bold"

    directory = Path(fonts_dir) if fonts_dir else None
    if not directory:
        directory = Path(__file__).resolve().parents[1] / "static" / "fonts"

    regular = directory / "Vazirmatn-Regular.ttf"
    bold = directory / "Vazirmatn-Bold.ttf"
    if not regular.exists():
        raise FileNotFoundError(
            f"فونت فارسی پیدا نشد: {regular}"
        )

    pdfmetrics.registerFont(TTFont("Vazirmatn", str(regular)))
    pdfmetrics.registerFont(
        TTFont("Vazirmatn-Bold", str(bold if bold.exists() else regular))
    )
    return "Vazirmatn", "Vazirmatn-Bold"


def persian_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


def shape(text):
    return _RESHAPER.reshape(str(text))


def rtl_line(text):
    return get_display(shape(text))


def wrap_rtl(text, font_name, font_size, max_width):
    lines = []
    for logical_line in str(text or "").split("\n"):
        words = [word for word in re.split(r"\s+", logical_line.strip()) if word]
        if not words:
            lines.append("")
            continue

        current = []
        for word in words:
            attempt = " ".join(current + [word])
            width = pdfmetrics.stringWidth(
                shape(attempt),
                font_name,
                font_size,
            )
            if current and width > max_width:
                lines.append(rtl_line(" ".join(current)))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(rtl_line(" ".join(current)))
    return lines


def rtl_paragraph(text, style, available_width):
    indent = (style.leftIndent or 0) + (style.rightIndent or 0)
    usable_width = max(24, available_width - indent - 2)
    lines = wrap_rtl(
        text,
        style.fontName,
        style.fontSize,
        usable_width,
    )
    return XPreformatted("\n".join(escape(line) for line in lines), style)

