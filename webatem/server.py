"""Where the server listens — the Companion-style "run on interface X, port
Y" setting — and the hooks the launcher registers so the Server settings
page can apply a change without a quit.

Resolution order for the address: the HOST / PORT environment variables
(Docker, a systemd unit: the environment is the configuration there) win
over ``server.json`` in the data dir (what the settings page writes), which
wins over the defaults (all interfaces, 8880).

``runtime`` is the seam between the web app and the process that hosts it.
The launcher registers a controller with ``restart(host, port)`` and the
start-at-login getters/setters; under plain ``uvicorn`` (the Docker image)
nothing is registered and the page says so.
"""
import json
import os
from pathlib import Path

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 8880   # not 8000: Bitfocus Companion lives there on the same desks
NO_RESTART_NOTE = ('Saved. This server was started with a fixed address (the HOST / PORT '
                   'environment, or plain uvicorn), so the change applies where that is set.')


def data_dir() -> Path:
    env = os.environ.get('DATA_DIR') or os.environ.get('WEBATEM_DATA_DIR')
    if env:
        return Path(env)
    from django.conf import settings
    return Path(settings.DATA_DIR)


def config_path() -> Path:
    return data_dir() / 'server.json'


def _saved() -> dict:
    try:
        saved = json.loads(config_path().read_text())
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    """The effective listen address and where each part came from, plus the
    launcher window's own preference (start minimized)."""
    host, port, source = DEFAULT_HOST, DEFAULT_PORT, 'default'
    saved = _saved()
    if saved.get('host'):
        host, source = str(saved['host']), 'file'
    if saved.get('port'):
        port, source = int(saved['port']), 'file'
    env_host, env_port = os.environ.get('HOST'), os.environ.get('PORT')
    if env_host or env_port:
        source = 'env'
        if env_host:
            host = env_host
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                pass
    return {'host': host, 'port': port, 'source': source,
            'start_minimized': bool(saved.get('start_minimized', False))}


def save(host: str, port: int, start_minimized=None) -> None:
    saved = _saved()
    saved.update({'host': host, 'port': int(port)})
    if start_minimized is not None:
        saved['start_minimized'] = bool(start_minimized)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=1) + '\n')


def lan_ip():
    """This machine's primary LAN address (no packet is sent). None if unknown."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def interfaces() -> list:
    """IPv4 addresses this machine could listen on, with the adapter's name,
    loopback last. Empty if the enumeration is unavailable."""
    try:
        import ifaddr
    except ImportError:
        return []
    found = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and ip.ip != '127.0.0.1':
                found.append({'ip': ip.ip, 'name': adapter.nice_name})
    found.sort(key=lambda e: (e['ip'].startswith('169.254.'), e['name'], e['ip']))
    found.append({'ip': '127.0.0.1', 'name': 'This computer only'})
    return found


def validate(host, port):
    """Return (host, port) or raise ValueError with a message for the page."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError('Port must be a number.')
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    host = str(host or '').strip()
    allowed = {DEFAULT_HOST, '127.0.0.1'} | {e['ip'] for e in interfaces()}
    if host not in allowed:
        raise ValueError('Pick one of the addresses this machine has.')
    return host, port


def url_for(host: str, port: int, seen_from: str = '') -> str:
    """The address a browser should use after a change. Listening on all
    interfaces keeps whatever host the browser already used; a specific
    address is that address."""
    if host == DEFAULT_HOST:
        name = (seen_from or '127.0.0.1').rsplit(':', 1)[0] or '127.0.0.1'
    else:
        name = host
    return f'http://{name}:{port}/atem/'


class _Runtime:
    """What the hosting process registered (see the launcher)."""

    def __init__(self):
        self.controller = None

    def register(self, controller) -> None:
        self.controller = controller

    def current(self):
        c = self.controller
        if c is not None:
            return (c.host, c.port)
        cfg = load()
        return (cfg['host'], cfg['port'])

    def restart_available(self) -> bool:
        return self.controller is not None and hasattr(self.controller, 'restart')

    def request_restart(self, host: str, port: int) -> None:
        self.controller.restart(host, port)

    def last_error(self):
        return getattr(self.controller, 'last_error', None)

    def startup_note(self):
        return getattr(self.controller, 'startup_note', None)

    def running(self) -> bool:
        # plain uvicorn is serving by definition; the launcher says
        return bool(getattr(self.controller, 'running', True))

    def restarting(self) -> bool:
        return bool(getattr(self.controller, 'restarting', False))

    def quit_available(self) -> bool:
        return self.controller is not None and hasattr(self.controller, 'quit')

    def quit(self) -> None:
        self.controller.quit()

    def autostart_available(self) -> bool:
        return self.controller is not None and hasattr(self.controller, 'set_autostart')

    def autostart_enabled(self) -> bool:
        return bool(self.controller.autostart_enabled()) if self.autostart_available() else False

    def set_autostart(self, enabled: bool) -> None:
        self.controller.set_autostart(bool(enabled))


runtime = _Runtime()


def describe() -> dict:
    """Everything the Server settings page shows."""
    cfg = load()
    host, port = runtime.current()
    chosen = host is not None                       # the launcher: nothing chosen yet -> no server, no address
    reach = host if host not in (DEFAULT_HOST, '', None) else (lan_ip() or '127.0.0.1')
    return {
        'host': host,
        'port': port,
        'source': cfg['source'],
        'start_minimized': cfg['start_minimized'],
        'url': f'http://{reach}:{port}/atem/' if chosen else None,        # what other devices open
        'local_url': f'http://127.0.0.1:{port}/atem/' if chosen else None,
        'interfaces': interfaces(),
        'restart_available': runtime.restart_available(),
        'last_error': runtime.last_error(),
        'startup_note': runtime.startup_note(),
        'running': runtime.running(),
        'restarting': runtime.restarting(),
        'quit_available': runtime.quit_available(),
        'autostart': {'available': runtime.autostart_available(), 'enabled': runtime.autostart_enabled()},
    }
