#!/usr/bin/env python3
"""WebATEM native launcher — run the web app locally, no Docker.

This is how a desktop user (Mac / Windows / Linux) runs WebATEM: the SAME
browser-based app, served by a local process that lives on your REAL
network — so ATEM mDNS discovery works. (A Docker container on Mac/Windows
sits behind a VM+NAT and can't see the LAN's multicast; a native process
can, exactly like ATEM Software Control.) Packaged into one self-contained
executable per OS via PyInstaller — no Python, no Docker required by the
user: download, double-click, browser opens, switchers appear.

The Docker image stays the path for a shared studio server (a Linux box
with a dedicated IP), where mDNS works natively too.

Env knobs (all optional): PORT (default 8000), HOST (default 0.0.0.0 so
other devices on the LAN can reach it), WEBATEM_DATA_DIR (override the
per-user data location), WEBATEM_NO_BROWSER=1 (don't auto-open a browser).
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _data_dir() -> Path:
    """A per-user, writable location for the SQLite DB, generated secret,
    and upload scratch. The frozen bundle itself is read-only, so runtime
    state must live outside it."""
    env = os.environ.get('WEBATEM_DATA_DIR') or os.environ.get('DATA_DIR')
    if env:
        path = Path(env)
    elif sys.platform == 'darwin':
        path = Path.home() / 'Library' / 'Application Support' / 'WebATEM'
    elif os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home())
        path = Path(base) / 'WebATEM'
    else:
        xdg = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
        path = Path(xdg) / 'WebATEM'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _wait_and_open(url: str, port: int) -> None:
    """Wait for uvicorn to accept connections, then open the browser once."""
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        return
    if os.environ.get('WEBATEM_NO_BROWSER') != '1':
        try:
            webbrowser.open(url)
        except Exception:
            pass


def main() -> None:
    data_dir = _data_dir()
    # settings.py reads DATA_DIR at import time — set it before django.setup().
    os.environ['DATA_DIR'] = str(data_dir)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ.setdefault('DEBUG', 'False')  # bundled static via WhiteNoise

    import django
    django.setup()

    from django.core.management import call_command
    call_command('migrate', '--noinput', verbosity=0)

    port = int(os.environ.get('PORT', '8000'))
    host = os.environ.get('HOST', '0.0.0.0')
    url = f'http://127.0.0.1:{port}/atem/'

    threading.Thread(target=_wait_and_open, args=(url, port), daemon=True).start()

    from config.asgi import application
    import uvicorn

    print(f'\n  WebATEM is running — open {url}\n  (Press Ctrl+C to quit.)\n', flush=True)
    uvicorn.run(application, host=host, port=port, log_level='info')


if __name__ == '__main__':
    main()
