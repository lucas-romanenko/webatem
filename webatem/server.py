"""Where the server listens — the Companion-style "run on interface X, port
Y" setting — and the hooks the launcher registers so the Server settings
page can apply a change without a quit.

Resolution order for the address: the HOST / PORT environment variables
(Docker, a systemd unit: the environment is the configuration there) win
over ``server.json`` in the data dir (what the settings page writes), which
wins over the defaults (all interfaces, 8000).

``runtime`` is the seam between the web app and the process that hosts it.
The launcher registers a controller with ``restart(host, port)`` and the
start-at-login getters/setters; under plain ``uvicorn`` (the Docker image)
nothing is registered and the page says so.
"""
import json
import os
from pathlib import Path

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 8000
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


def load() -> dict:
    """The effective listen address and where each part came from."""
    host, port, source = DEFAULT_HOST, DEFAULT_PORT, 'default'
    try:
        saved = json.loads(config_path().read_text())
        if isinstance(saved, dict):
            if saved.get('host'):
                host, source = str(saved['host']), 'file'
            if saved.get('port'):
                port, source = int(saved['port']), 'file'
    except (OSError, ValueError):
        pass
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
    return {'host': host, 'port': port, 'source': source}


def save(host: str, port: int) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'host': host, 'port': int(port)}, indent=1) + '\n')


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
    return {
        'host': host,
        'port': port,
        'source': cfg['source'],
        'interfaces': interfaces(),
        'restart_available': runtime.restart_available(),
        'last_error': runtime.last_error(),
        'autostart': {'available': runtime.autostart_available(), 'enabled': runtime.autostart_enabled()},
    }
