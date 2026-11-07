from main import app
from core.admin_tools import set_admin_password


if __name__ == "__main__":
    set_admin_password(app)
