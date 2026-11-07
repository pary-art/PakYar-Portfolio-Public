from core.time_utils import format_jalali


def register_jalali_filter(app):
    """Register the Jalali date and Iran-time filter for Jinja templates."""
    app.jinja_env.filters["jalali"] = format_jalali
