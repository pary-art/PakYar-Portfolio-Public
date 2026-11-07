"""ساخت خروجی‌های Excel و PDF برای گزارش‌های پاکیار."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from core.pdf_utils import (
    persian_digits,
    register_persian_fonts,
    rtl_line,
    rtl_paragraph,
)
from core.time_utils import format_jalali


BRAND_DARK = "123B52"
BRAND_TEAL = "0F766E"
BRAND_LIGHT = "EAF4F5"
GREEN = "DCFCE7"
YELLOW = "FEF3C7"
RED = "FEE2E2"
PURPLE = "EFE9F8"
GRAY = "E2E8F0"


def _cost(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_label(status):
    return {
        "pending": "در انتظار بررسی",
        "approved": "تأیید شده",
        "rejected": "رد شده",
        "resubmitted": "ثبت مجدد شده",
        "reviewed": "بررسی شده",
    }.get(status, status or "-")


def _report_label(report):
    source = getattr(report, "resubmission_source", None)
    if source and report.status == "approved":
        return "تأیید پس از ثبت مجدد"
    if source and report.status == "rejected":
        return "رد پس از ثبت مجدد"
    if source or report.status == "resubmitted":
        return "ثبت مجدد شده"
    return _status_label(report.status)


def _style_workbook(wb):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style="thin", color="D8DEE4")
    border = Border(bottom=thin)
    for worksheet in wb.worksheets:
        worksheet.sheet_view.rightToLeft = True
        worksheet.sheet_view.showGridLines = False
        for row in worksheet.iter_rows():
            for cell in row:
                cell.font = Font(name="Tahoma", size=10)
                cell.alignment = Alignment(
                    horizontal="right",
                    vertical="center",
                    wrap_text=True,
                )
                cell.border = border

    return {
        "Alignment": Alignment,
        "Border": Border,
        "Font": Font,
        "PatternFill": PatternFill,
        "Side": Side,
        "border": border,
    }


def _style_header(worksheet, row_number, styles, fill=BRAND_DARK):
    from openpyxl.styles import Alignment

    for cell in worksheet[row_number]:
        cell.fill = styles["PatternFill"]("solid", fgColor=fill)
        cell.font = styles["Font"](
            name="Tahoma",
            size=10,
            bold=True,
            color="FFFFFF",
        )
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )
    worksheet.row_dimensions[row_number].height = 30


def _set_widths(worksheet, widths):
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width


def _save_workbook(wb):
    wb.calculation.calcMode = "auto"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def build_reports_xlsx(
    reports,
    month_label,
    scope_label,
    date_formatter=None,
):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    date_formatter = date_formatter or format_jalali
    workbook = Workbook()
    summary = workbook.active
    summary.title = "خلاصه مدیریتی"
    details = workbook.create_sheet("گزارش‌های نظافت")
    sections = workbook.create_sheet("خلاصه بخش‌ها")
    styles = _style_workbook(workbook)

    summary["A1"] = "گزارش مدیریتی نظافت"
    summary["A2"] = f"بازه: {month_label}"
    summary["A3"] = f"محدوده: {scope_label}"
    summary["A5"] = "شاخص"
    summary["B5"] = "مقدار"
    summary["A6"] = "کل گزارش‌ها"
    summary["A7"] = "تأیید شده"
    summary["A8"] = "رد شده"
    summary["A9"] = "در انتظار بررسی"
    summary["A10"] = "ثبت مجدد شده"
    summary["A11"] = "نرخ تأیید"
    _style_header(summary, 5, styles)

    headers = [
        "شماره",
        "ثبت‌کننده",
        "بخش",
        "شیفت",
        "شرح فعالیت",
        "وضعیت",
        "بازخورد",
        "تاریخ ثبت",
    ]
    details.append(headers)
    _style_header(details, 1, styles)
    for report in reports:
        details.append(
            [
                report.id,
                report.user.full_name if report.user else "-",
                report.section.name if report.section else "-",
                report.shift or "-",
                report.description or "-",
                _report_label(report),
                report.supervisor_feedback or "-",
                date_formatter(report.created_at),
            ]
        )

    last_row = max(details.max_row, 2)
    summary["B6"] = f"=COUNTA('گزارش‌های نظافت'!A2:A{last_row})"
    summary["B7"] = (
        f'=COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"تأیید شده")'
        f'+COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"تأیید پس از ثبت مجدد")'
    )
    summary["B8"] = (
        f'=COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"رد شده")'
        f'+COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"رد پس از ثبت مجدد")'
    )
    summary["B9"] = (
        f'=COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"در انتظار بررسی")'
        f'+COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"در انتظار بررسی پس از ثبت مجدد")'
    )
    summary["B10"] = (
        f'=COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"ثبت مجدد شده")'
        f'+COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"تأیید پس از ثبت مجدد")'
        f'+COUNTIF(\'گزارش‌های نظافت\'!F2:F{last_row},"رد پس از ثبت مجدد")'
    )
    summary["B11"] = '=IFERROR(B7/B6,0)'
    summary["B11"].number_format = "0.0%"
    for cell in summary["A"]:
        cell.font = styles["Font"](name="Tahoma", size=10, bold=True)
    summary["A1"].font = styles["Font"](
        name="Tahoma",
        size=16,
        bold=True,
        color=BRAND_DARK,
    )

    section_counts = {}
    for report in reports:
        name = report.section.name if report.section else "-"
        section_counts.setdefault(name, {"total": 0, "approved": 0, "rejected": 0})
        section_counts[name]["total"] += 1
        if report.status == "approved":
            section_counts[name]["approved"] += 1
        if report.status == "rejected":
            section_counts[name]["rejected"] += 1

    sections.append(["بخش", "کل گزارش", "تأیید شده", "رد شده"])
    _style_header(sections, 1, styles, BRAND_TEAL)
    for name, values in sorted(section_counts.items()):
        sections.append(
            [name, values["total"], values["approved"], values["rejected"]]
        )

    if len(section_counts):
        chart = BarChart()
        chart.type = "bar"
        chart.style = 10
        chart.title = "مقایسه گزارش‌های بخش‌ها"
        chart.y_axis.title = "بخش"
        chart.x_axis.title = "تعداد"
        data = Reference(sections, min_col=2, max_col=4, min_row=1, max_row=sections.max_row)
        categories = Reference(sections, min_col=1, min_row=2, max_row=sections.max_row)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(categories)
        chart.height = 8
        chart.width = 15
        summary.add_chart(chart, "D5")

    details.freeze_panes = "A2"
    details.auto_filter.ref = details.dimensions
    sections.freeze_panes = "A2"
    sections.auto_filter.ref = sections.dimensions
    _set_widths(summary, {"A": 30, "B": 18, "D": 22})
    _set_widths(
        details,
        {
            "A": 10,
            "B": 22,
            "C": 20,
            "D": 12,
            "E": 45,
            "F": 24,
            "G": 35,
            "H": 22,
        },
    )
    _set_widths(sections, {"A": 24, "B": 15, "C": 15, "D": 15})
    return _save_workbook(workbook)


def build_supply_xlsx(
    usages,
    section_data,
    month_label,
    scope_label,
    date_formatter=None,
):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    date_formatter = date_formatter or format_jalali
    workbook = Workbook()
    summary = workbook.active
    summary.title = "خلاصه مصرف"
    details = workbook.create_sheet("مصرف شیفت")
    sections = workbook.create_sheet("خلاصه بخش‌ها")
    styles = _style_workbook(workbook)

    summary["A1"] = "گزارش هزینه و مصرف مواد و تجهیزات"
    summary["A2"] = f"بازه: {month_label}"
    summary["A3"] = f"محدوده: {scope_label}"
    summary.append([])
    summary.append(["شاخص", "مقدار"])
    _style_header(summary, 5, styles)
    summary.append(["تعداد ثبت‌های مصرف", None])
    summary.append(["هزینهٔ تقریبی ثبت‌شده (تومان)", None])
    summary.append(["بررسی شده", None])
    summary.append(["در انتظار بررسی", None])

    headers = [
        "شماره",
        "تاریخ ثبت",
        "ثبت‌کننده",
        "بخش",
        "شیفت",
        "مواد شوینده و مصرفی",
        "لوازم و تجهیزات",
        "هزینهٔ تقریبی (تومان)",
        "توضیحات",
        "وضعیت",
        "بازخورد مسئول بخش",
    ]
    details.append(headers)
    _style_header(details, 1, styles)
    for usage in usages:
        details.append(
            [
                usage.id,
                date_formatter(usage.created_at),
                usage.user.full_name if usage.user else "-",
                usage.section.name if usage.section else "-",
                usage.shift or "-",
                usage.materials or "-",
                usage.equipment or "-",
                _cost(getattr(usage, "estimated_cost", 0)),
                usage.notes or "-",
                _status_label(usage.status),
                usage.supervisor_feedback or "-",
            ]
        )

    last_row = max(details.max_row, 2)
    summary["B6"] = f"=COUNTA('مصرف شیفت'!A2:A{last_row})"
    summary["B7"] = f"=SUM('مصرف شیفت'!H2:H{last_row})"
    summary["B8"] = f'=COUNTIF(\'مصرف شیفت\'!J2:J{last_row},"بررسی شده")'
    summary["B9"] = f'=COUNTIF(\'مصرف شیفت\'!J2:J{last_row},"در انتظار بررسی")'
    summary["B7"].number_format = '#,##0 "تومان"'
    summary["A1"].font = styles["Font"](
        name="Tahoma",
        size=16,
        bold=True,
        color=BRAND_DARK,
    )

    sections.append(["بخش", "تعداد ثبت", "هزینهٔ تقریبی (تومان)", "مواد", "تجهیزات"])
    _style_header(sections, 1, styles, BRAND_TEAL)
    for item in section_data:
        sections.append(
            [
                item["name"],
                item["total"],
                _cost(item.get("cost", 0)),
                item["materials"],
                item["equipment"],
            ]
        )
    for row in range(2, sections.max_row + 1):
        sections.cell(row, 3).number_format = '#,##0 "تومان"'

    if section_data:
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = "هزینهٔ تقریبی مصرف به تفکیک بخش"
        chart.y_axis.title = "تومان"
        chart.x_axis.title = "بخش"
        data = Reference(sections, min_col=3, max_col=3, min_row=1, max_row=sections.max_row)
        categories = Reference(sections, min_col=1, min_row=2, max_row=sections.max_row)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(categories)
        chart.height = 8
        chart.width = 15
        summary.add_chart(chart, "D5")

    details.freeze_panes = "A2"
    details.auto_filter.ref = details.dimensions
    sections.freeze_panes = "A2"
    sections.auto_filter.ref = sections.dimensions
    _set_widths(summary, {"A": 34, "B": 22, "D": 22})
    _set_widths(
        details,
        {
            "A": 10,
            "B": 22,
            "C": 22,
            "D": 20,
            "E": 12,
            "F": 70,
            "G": 55,
            "H": 24,
            "I": 35,
            "J": 20,
            "K": 35,
        },
    )
    _set_widths(sections, {"A": 24, "B": 15, "C": 25, "D": 15, "E": 15})
    return _save_workbook(workbook)


def _pdf_styles(font_name, bold_font_name):
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle(
        "PersianBody",
        fontName=font_name,
        fontSize=8.5,
        leading=14,
        alignment=TA_RIGHT,
        textColor="#1F2937",
    )
    small = ParagraphStyle(
        "PersianSmall",
        parent=body,
        fontSize=7.5,
        leading=11,
    )
    heading = ParagraphStyle(
        "PersianHeading",
        parent=body,
        fontName=bold_font_name,
        fontSize=13,
        leading=20,
        textColor="#123B52",
    )
    return body, small, heading


def _pdf_table_style(font_name="Vazirmatn", bold_font_name="Vazirmatn-Bold"):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123B52")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTNAME", (0, 0), (-1, 0), bold_font_name),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE4")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7F9")]),
        ]
    )


def _build_pdf(title, subtitle, summary_rows, detail_headers, detail_rows, font_dir):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font_name, bold_font_name = register_persian_fonts(font_dir)
    body, small, heading = _pdf_styles(font_name, bold_font_name)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=title,
        author="PakYar",
    )
    story = [
        rtl_paragraph(title, heading, document.width),
        rtl_paragraph(subtitle, body, document.width),
        Spacer(1, 8 * mm),
    ]

    summary_data = [
        [
            rtl_line(label),
            rtl_line(value),
        ]
        for label, value in summary_rows
    ]
    summary_table = Table(
        [[rtl_line("شاخص"), rtl_line("مقدار")]] + summary_data,
        colWidths=[75 * mm, 55 * mm],
        repeatRows=1,
    )
    summary_table.setStyle(_pdf_table_style(font_name, bold_font_name))
    story.extend([summary_table, Spacer(1, 8 * mm)])

    cell_widths = [
        document.width / len(detail_headers)
        for _ in detail_headers
    ]
    if len(detail_headers) == 8:
        cell_widths = [15 * mm, 24 * mm, 28 * mm, 27 * mm, 58 * mm, 30 * mm, 45 * mm, 27 * mm]
    elif len(detail_headers) == 11:
        cell_widths = [13 * mm, 22 * mm, 24 * mm, 23 * mm, 14 * mm, 42 * mm, 42 * mm, 27 * mm, 35 * mm, 24 * mm, 35 * mm]

    header_cells = [rtl_line(header) for header in detail_headers]
    detail_data = [header_cells]
    for row in detail_rows:
        detail_data.append(
            [
                rtl_paragraph(str(value or "-"), small, width - 10)
                for value, width in zip(row, cell_widths)
            ]
        )

    details_table = Table(
        detail_data,
        colWidths=cell_widths,
        repeatRows=1,
        splitByRow=1,
    )
    details_table.setStyle(_pdf_table_style(font_name, bold_font_name))
    story.append(details_table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawRightString(
            landscape(A4)[0] - 16 * mm,
            8 * mm,
            rtl_line("سامانه پاکیار · گزارش مدیریتی"),
        )
        canvas.drawString(
            16 * mm,
            8 * mm,
            rtl_line(f"صفحه {persian_digits(doc.page)}"),
        )
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return buffer


def build_reports_pdf(
    reports,
    month_label,
    scope_label,
    font_dir=None,
    date_formatter=None,
):
    date_formatter = date_formatter or format_jalali
    rows = []
    for report in reports:
        rows.append(
            [
                report.id,
                report.user.full_name if report.user else "-",
                report.section.name if report.section else "-",
                report.shift or "-",
                report.description or "-",
                _report_label(report),
                report.supervisor_feedback or "-",
                date_formatter(report.created_at),
            ]
        )

    approved = sum(report.status == "approved" for report in reports)
    rejected = sum(report.status == "rejected" for report in reports)
    pending = sum(report.status == "pending" for report in reports)
    return _build_pdf(
        "گزارش مدیریتی گزارش‌های نظافت",
        f"بازهٔ گزارش: {month_label} · محدوده: {scope_label}",
        [
            ("کل گزارش‌ها", persian_digits(len(reports))),
            ("تأیید شده", persian_digits(approved)),
            ("رد شده", persian_digits(rejected)),
            ("در انتظار بررسی", persian_digits(pending)),
        ],
        [
            "شماره",
            "ثبت‌کننده",
            "بخش",
            "شیفت",
            "شرح فعالیت",
            "وضعیت",
            "بازخورد",
            "تاریخ ثبت",
        ],
        rows,
        font_dir,
    )


def build_supply_pdf(
    usages,
    section_data,
    month_label,
    scope_label,
    font_dir=None,
    date_formatter=None,
):
    date_formatter = date_formatter or format_jalali
    rows = []
    for usage in usages:
        rows.append(
            [
                usage.id,
                date_formatter(usage.created_at),
                usage.user.full_name if usage.user else "-",
                usage.section.name if usage.section else "-",
                usage.shift or "-",
                usage.materials or "-",
                usage.equipment or "-",
                f"{_cost(getattr(usage, 'estimated_cost', 0)):,.0f} تومان",
                usage.notes or "-",
                _status_label(usage.status),
                usage.supervisor_feedback or "-",
            ]
        )

    total_cost = sum(
        _cost(getattr(usage, "estimated_cost", 0))
        for usage in usages
    )
    reviewed = sum(usage.status == "reviewed" for usage in usages)
    return _build_pdf(
        "گزارش هزینه و مصرف مواد و تجهیزات",
        f"بازهٔ گزارش: {month_label} · محدوده: {scope_label}",
        [
            ("تعداد ثبت‌های مصرف", persian_digits(len(usages))),
            (
                "هزینهٔ تقریبی ثبت‌شده",
                f"{persian_digits(f'{total_cost:,.0f}')} تومان",
            ),
            ("بررسی شده", persian_digits(reviewed)),
            ("در انتظار بررسی", persian_digits(len(usages) - reviewed)),
        ],
        [
            "شماره",
            "تاریخ",
            "ثبت‌کننده",
            "بخش",
            "شیفت",
            "مواد",
            "تجهیزات",
            "هزینه",
            "توضیحات",
            "وضعیت",
            "بازخورد",
        ],
        rows,
        font_dir,
    )
