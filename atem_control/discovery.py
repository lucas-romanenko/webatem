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

# mDNS service the ATEMs advertise under — this is the ATEM-SPECIFIC type
# and every instance IS a switcher (no class filter needed). The instance
# name IS the operator-set switcher name, so discovery is fully passive:
# names come straight off the announcement, zero switcher contact, exactly
# like ATEM Software Control. (NOT ``_blackmagic._tcp`` — that's the
# Ultimattes/other BMD gear; the ATEMs are on ``_switcher_ctrl._udp``.
# Empirically verified on the studio VLAN: 250 named ATEMs here.)
_MDNS_SERVICE = "_switcher_ctrl._udp.local."

# ATEM control port + the wire handshake bytes. A client hello is a SYN
# (flag 0x02) carrying opcode 0x01; the switcher replies SYN opcode 0x02;
# our goodbye is SYN opcode 0x04 (see atemwire.transport.close_session).
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


def _instance_name(service_name: str) -> str:
    """The operator-set switcher name from an mDNS instance name. Strips
    the service suffix and un-escapes DNS label dots (``1\\.1`` → ``1.1``)
    so a room named 'ATEM TO-BC 1.1 CL 01' reads back with its dots."""
    friendly = service_name.replace('.' + _MDNS_SERVICE, '').replace(_MDNS_SERVICE, '').rstrip('.')
    return friendly.replace('\\.', '.').replace('\\032', ' ')


class _AtemListener:
    """Zeroconf ServiceListener that mirrors ATEM ``_switcher_ctrl._udp``
    announcements into ``_registry``. Every instance IS an ATEM (the
    service type is ATEM-specific), and the instance name IS the switcher
    name — no class filter, no handshake. Runs on zeroconf's own thread."""

    def _resolve(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=2500)
        except Exception:
            return
        if not info:
            return
        addrs = []
        try:
            addrs = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
        except Exception:
            pass
        if not addrs:
            return
        props = {_decode(k): _decode(v) for k, v in (info.properties or {}).items() if k}
        friendly = _instance_name(name)
        # This service's TXT carries only 'unique id' (no model); the name
        # is authoritative. Dedup by unique id so one switcher reachable at
        # several addresses collapses to one entry.
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
        friendly = _instance_name(name)
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

# Largest network we'll auto-sweep: a /22 is 1024 addresses (~1s of
# hellos), which covers a facility spread across several /24s on one
# subnet (the studio VLAN here is a /22). Anything wider than this (a
# /16, say) we clamp to the host's /24 rather than firing 65k hellos —
# the manual subnet field can target the rest.
_MAX_SWEEP_HOSTS = 1024


def _primary_ipv4() -> str | None:
    """This host's primary outbound IPv4 (no packet is sent — connect()
    on a UDP socket just resolves the source address the OS would use)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _iface_netmask(ifname: str) -> str | None:
    """The IPv4 netmask of ``ifname`` via SIOCGIFNETMASK (Linux). None on
    any error / non-Linux."""
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack('256s', ifname.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891b, packed)[20:24])
    except OSError:
        return None
    finally:
        s.close()


def local_network():
    """The host's own IPv4 network as an ``ipaddress.IPv4Network`` using
    the interface's REAL prefix (a /22 studio VLAN is one network, not
    four /24s). Falls back to the /24 of the primary IP if the mask can't
    be read. None if no address is found."""
    import ipaddress
    ip = _primary_ipv4()
    if not ip:
        return None
    # Find the interface that carries this address and read its mask.
    try:
        for _idx, name in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                import fcntl
                packed = struct.pack('256s', name.encode()[:15])
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
            except OSError:
                continue
            finally:
                s.close()
            if addr == ip:
                mask = _iface_netmask(name)
                if mask:
                    return ipaddress.IPv4Network(f'{ip}/{mask}', strict=False)
                break
    except Exception:
        pass
    return ipaddress.IPv4Network(f'{ip}/24', strict=False)


def local_subnet() -> str | None:
    """Display label for the host's own subnet, e.g. ``'192.168.80.0/22'``
    (or the /24 prefix string on fallback). Used by the Connect page."""
    net = local_network()
    return str(net) if net else None


def sweep_ips(ips, *, settle: float = 2.0) -> list[str]:
    """Send the light ATEM hello to each IP in ``ips`` and return those
    that answered like an ATEM. Half-open only — a goodbye is sent to
    every responder, no session is established. ``settle`` is how long to
    keep collecting replies after the last hello goes out."""
    ips = list(ips)
    if not ips:
        return []

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
        for n, ip in enumerate(ips, 1):
            try:
                sock.sendto(_HELLO, (ip, _ATEM_PORT))
            except OSError:
                pass
            if n % 16 == 0:
                drain()
                time.sleep(0.002)
        deadline = time.time() + max(0.2, settle)
        while time.time() < deadline:
            drain()
            time.sleep(0.05)
    finally:
        sock.close()

    return sorted(responders, key=lambda s: tuple(int(o) for o in s.split('.')))


def _hosts_for(subnet: str | None):
    """The list of host IPs to sweep. A 3-octet prefix like ``192.168.81``
    means that /24; a full CIDR like ``192.168.80.0/22`` means that
    network; None means the host's own network (real prefix, clamped to
    _MAX_SWEEP_HOSTS)."""
    import ipaddress
    if subnet:
        subnet = subnet.strip().rstrip('.')
        try:
            if '/' in subnet:
                net = ipaddress.IPv4Network(subnet, strict=False)
            else:
                parts = subnet.split('.')
                if len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    net = ipaddress.IPv4Network(f'{subnet}.0/24')
                else:
                    return []
        except ValueError:
            return []
    else:
        net = local_network()
        if net is None:
            return []
        # Too wide to auto-sweep — clamp to the host's /24.
        if net.num_addresses > _MAX_SWEEP_HOSTS:
            ip = _primary_ipv4()
            net = ipaddress.IPv4Network(f'{ip}/24', strict=False)
    return [str(h) for h in net.hosts()]


def scan_atems(subnet: str | None = None) -> dict:
    """Sweep a subnet and fold in any mDNS-known names. Returns
    ``{'subnet', 'atems': [{'ip', 'name', 'model', 'source'}]}``. When
    ``subnet`` is None the host's own network (real prefix) is used."""
    hosts = _hosts_for(subnet)
    if not hosts:
        return {'subnet': subnet, 'atems': []}
    label = subnet or local_subnet()
    ips = sweep_ips(hosts)
    atems = []
    for ip in ips:
        name, model = _name_for_ip(ip)
        atems.append({
            'ip': ip,
            'name': name,          # '' → the UI shows the bare IP
            'model': model,
            'source': 'mdns' if name else 'scan',
        })
    return {'subnet': label, 'atems': atems}
