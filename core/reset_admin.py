import os

from werkzeug.security import generate_password_hash

from main import app
from core.admin_tools import requested_admin_password
from core.models import Section, User, db


if os.environ.get("PAKYAR_RESET_DATABASE") != "YES":
    raise SystemExit(
        "برای بازسازی کامل دیتابیس، متغیر PAKYAR_RESET_DATABASE را برابر YES قرار دهید."
    )

admin_password = requested_admin_password()

with app.app_context():
    db.drop_all()
    db.create_all()

    admin_user = User(
        username="admin",
        password=generate_password_hash(admin_password),
        full_name="مسئول ارشد خدمات سامانه",
        role="manager",
    )
    db.session.add(admin_user)

    sections = [
        Section(name="بخش اورژانس", floor="همکف", description="نظافت ۲۴ ساعته"),
        Section(name="بخش جراحی", floor="طبقه دوم", description="استریل دقیق"),
        Section(
            name="بخش مراقبت‌های ویژه (ICU)",
            floor="طبقه اول",
            description="ضدعفونی مداوم",
        ),
    ]
    db.session.add_all(sections)
    db.session.commit()

print("دیتابیس از نو ساخته شد و حساب مسئول خدمات ایجاد گردید.")
print("نام کاربری: admin")
print("رمز عبور از ورودی امن دریافت شد.")
