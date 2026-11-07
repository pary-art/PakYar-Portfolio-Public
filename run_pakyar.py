"""Launch the local PakYar server and open it in the default browser."""

from __future__ import annotations

import os
import threading
import webbrowser

from main import app

try:
    from waitress import serve
except ImportError:  # pragma: no cover - only for an incomplete dev install
    serve = None


URL = app.config["PAKYAR_BROWSER_URL"]


def open_browser() -> None:
    webbrowser.open(URL)


if __name__ == "__main__":
    if os.environ.get("PAKYAR_OPEN_BROWSER", "1").strip() != "0":
        threading.Timer(1.2, open_browser).start()

    if serve is None:
        # Keep source development usable when requirements have not been
        # installed yet. Production/network packages use Waitress below.
        app.run(
            debug=False,
            host=app.config["PAKYAR_HOST"],
            port=app.config["PAKYAR_PORT"],
            threaded=True,
            use_reloader=False,
        )
    else:
        serve(
            app,
            host=app.config["PAKYAR_HOST"],
            port=app.config["PAKYAR_PORT"],
            threads=app.config["PAKYAR_THREADS"],
            channel_timeout=90,
            ident="PakYar",
        )
