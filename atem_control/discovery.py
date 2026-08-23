"""LAN discovery of ATEM switchers for the Connect page.

Two complementary methods, mirroring how ATEM Software Control finds
switchers on a network:

  * **Passive mDNS/Bonjour listening** — a background thread watches
    ``_blackmagic._tcp`` for ``AtemSwitcher`` announcements and keeps a
    live registry (friendly name + IP + model). It sends NOTHING to any
    switcher; it only listens for the multicast the ATEMs already emit.
    This is the zero-touch "open it and they're there" default.

  * **On-demand subnet sweep** — a light UDP "hello" (the client
    handshake opcode ``0x01``) to every host on a /24. Each real ATEM
    answers with its handshake reply; we send an immediate protocol
    goodbye and never complete the third leg, so NO session is ever
    established on the switcher — it's strictly lighter than a normal
    connect. Returns the responding IPs; names are filled from the mDNS
    registry where known, otherwise resolved lazily when the operator
    actually clicks to connect. This catches ATEMs that don't advertise
    over mDNS (common across VLANs / VPNs where multicast is dropped).

Both require the container to share the host network (compose
``network_mode: host``). On a bridge network neither reaches the LAN —
mDNS multicast never arrives and a sweep would scan the docker subnet.

Nothing here raises to callers: mDNS unavailable (no ``zeroconf``, no
multicast) degrades to an empty registry, and a sweep on a dead network
just returns ``[]``.
"""

import logging
import socket
import struct
import threading
import time

logger = logging.getLogger(__name__)

# Blackmagic mDNS service the ATEMs (and Ultimattes, HyperDecks, …)
# announce themselves under; we filter on the TXT ``class`` field.
_MDNS_SERVICE = "_blackmagic._tcp.local."
_ATEM_CLASS = "AtemSwitcher"

# ATEM control port + the wire handshake bytes. A client hello is a SYN
# (flag 0x02) carrying opcode 0x01; the switcher replies SYN opcode 0x02;
# our goodbye is SYN opcode 0x04 (see pyatem.transport.close_session).
_ATEM_PORT = 9910
_FLAG_SYN = 2
_HELLO = struct.pack('>HHH2xHH', 20 | (_FLAG_SYN << 11), 0x1337, 0, 0, 0) \
    + bytes([0x01, 0, 0, 0, 0, 0, 0, 0])
_GOODBYE = struct.pack('>HHH2xHH', 20 | (_FLAG_SYN << 11), 0x1337, 0, 0, 0) \
    + bytes([0x04, 0, 0, 0, 0, 0, 0, 0])


# --------------------------------------------------------------------------
# Passive mDNS registry
# --------------------------------------------------------------------------

# key (unique-id, or IP as fallback) -> {'ip', 'name', 'model'}
_registry: dict[str, dict] = {}
_registry_lock = threading.Lock()
_browser = None          # keeps the ServiceBrowser + Zeroconf alive
_zeroconf = None
_start_lock = threading.Lock()
_started = False
_mdns_available: bool | None = None   # None = not yet attempted


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else (value or '')


class _AtemListener:
    """Zeroconf ServiceListener that mirrors AtemSwitcher announcements
    into ``_registry``. Runs on zeroconf's own thread."""

    def _resolve(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
        except Exception:
            return
        if not info:
            return
        props = {_decode(k): _decode(v) for k, v in (info.properties or {}).items() if k}
        if props.get('class') != _ATEM_CLASS:
            return
        addrs = []
        try:
            addrs = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
        except Exception:
            pass
        if not addrs:
            return
        friendly = name.replace('.' + _MDNS_SERVICE, '').replace(_MDNS_SERVICE, '').rstrip('.')
        key = props.get('unique id') or addrs[0]
        with _registry_lock:
            _registry[key] = {
                'ip': addrs[0],
                'name': friendly or addrs[0],
                'model': props.get('name', ''),
            }

    def add_service(self, zc, type_, name):
        self._resolve(zc, type_, name)

    def update_service(self, zc, type_, name):
        self._resolve(zc, type_, name)

    def remove_service(self, zc, type_, name):
        friendly = name.replace('.' + _MDNS_SERVICE, '').replace(_MDNS_SERVICE, '').rstrip('.')
        with _registry_lock:
            for key, entry in list(_registry.items()):
                if entry['name'] == friendly:
                    del _registry[key]


def ensure_mdns_started() -> bool:
    """Start the passive mDNS browser once, lazily. Idempotent and
    thread-safe. Returns True if mDNS is available (browser running),
    False if ``zeroconf`` is missing or the socket couldn't be opened.

    Lazy-started on the first Connect-page poll rather than at app
    ready() so management commands (migrate/collectstatic) never spin up
    a multicast listener.
    """
    global _browser, _zeroconf, _started, _mdns_available
    if _started:
        return bool(_mdns_available)
    with _start_lock:
        if _started:
            return bool(_mdns_available)
        _started = True
        try:
            from zeroconf import Zeroconf, ServiceBrowser
            _zeroconf = Zeroconf()
            _browser = ServiceBrowser(_zeroconf, _MDNS_SERVICE, _AtemListener())
            _mdns_available = True
            logger.info("ATEM mDNS discovery started (%s)", _MDNS_SERVICE)
        except Exception as e:
            _mdns_available = False
            logger.warning("ATEM mDNS discovery unavailable: %s", e)
        return bool(_mdns_available)


def discovered_atems() -> list[dict]:
    """Snapshot of the mDNS registry, sorted by name. Each entry:
    ``{'ip', 'name', 'model', 'source': 'mdns'}``."""
    with _registry_lock:
        out = [
            {'ip': e['ip'], 'name': e['name'], 'model': e['model'], 'source': 'mdns'}
            for e in _registry.values()
        ]
    out.sort(key=lambda e: (e['name'] or e['ip']).lower())
    return out


def _name_for_ip(ip: str) -> tuple[str, str]:
    """(name, model) from the mDNS registry for an IP, or ('', '')."""
    with _registry_lock:
        for e in _registry.values():
            if e['ip'] == ip:
                return e['name'], e['model']
    return '', ''


# --------------------------------------------------------------------------
# On-demand subnet sweep
# --------------------------------------------------------------------------

def local_subnet() -> str | None:
    """The /24 prefix (first three octets) of this host's primary
    outbound interface, e.g. ``'192.168.81'``. None if it can't be
    determined."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent — connect() on a UDP socket just picks the
        # route/source address the OS would use to reach that dest.
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()
    parts = ip.split('.')
    if len(parts) != 4:
        return None
    return '.'.join(parts[:3])


def sweep_subnet(subnet: str, *, settle: float = 2.0) -> list[str]:
    """Send the light hello to every host on ``subnet`` (a 3-octet /24
    prefix like ``'192.168.81'``) and return the IPs that answered like
    an ATEM, sorted numerically. Half-open only — a goodbye is sent to
    every responder, no session is established. ``settle`` is how long to
    keep collecting replies after the last hello goes out."""
    parts = subnet.strip().rstrip('.').split('.')
    if len(parts) != 3 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return []
    prefix = '.'.join(parts)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', 0))
    sock.setblocking(False)
    responders: dict[str, bool] = {}

    def drain():
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except BlockingIOError:
                return
            except OSError:
                return
            if len(data) < 13:
                continue
            word = struct.unpack_from('>H', data)[0]
            if not ((word >> 11) & _FLAG_SYN):
                continue
            if data[12] != 0x02:      # ATEM handshake reply opcode
                continue
            ip = addr[0]
            if ip not in responders:
                responders[ip] = True
                try:
                    sock.sendto(_GOODBYE, addr)
                except OSError:
                    pass

    try:
        for i in range(1, 255):
            try:
                sock.sendto(_HELLO, (f'{prefix}.{i}', _ATEM_PORT))
            except OSError:
                pass
            if i % 16 == 0:
                drain()
                time.sleep(0.002)
        deadline = time.time() + max(0.2, settle)
        while time.time() < deadline:
            drain()
            time.sleep(0.05)
    finally:
        sock.close()

    return sorted(responders, key=lambda s: int(s.rsplit('.', 1)[1]))


def scan_atems(subnet: str | None = None) -> dict:
    """Run a subnet sweep and fold in any mDNS-known names. Returns
    ``{'subnet', 'atems': [{'ip', 'name', 'model', 'source'}]}``. When
    ``subnet`` is None the host's own /24 is used."""
    subnet = subnet or local_subnet()
    if not subnet:
        return {'subnet': None, 'atems': []}
    ips = sweep_subnet(subnet)
    atems = []
    for ip in ips:
        name, model = _name_for_ip(ip)
        atems.append({
            'ip': ip,
            'name': name,          # '' → the UI shows the bare IP
            'model': model,
            'source': 'mdns' if name else 'scan',
        })
    return {'subnet': subnet, 'atems': atems}
