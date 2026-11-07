"""Safe command-line helpers for creating or resetting a manager account."""

from __future__ import annotations

import os
from getpass import getpass

from werkzeug.security import generate_password_hash

from core.models import User, db


def requested_admin_password() -> str:
    password = os.environ.get("PAKYAR_ADMIN_PASSWORD", "").strip()
    if not password:
        password = getpass("رمز جدید مسئول خدمات را وارد کنید: ").strip()

    if len(password) < 8:
        raise SystemExit("رمز مسئول خدمات باید حداقل ۸ کاراکتر باشد.")

    return password


def set_admin_password(app, username: str = "admin") -> None:
    password = requested_admin_password()

    with app.app_context():
        db.create_all()
        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(
                username=username,
            full_name="مسئول ارشد خدمات سامانه",
                role="manager",
                password=generate_password_hash(password),
            )
            db.session.add(user)
        else:
            user.password = generate_password_hash(password)
            user.role = "manager"

        db.session.commit()

    print(f"حساب مسئول خدمات «{username}» با موفقیت آماده شد.")
