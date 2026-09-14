"""ATEM REST config API client — read device info / set the stored name.

Newer ATEMs (with the web admin) expose a REST API on HTTP port 80 —
``http://<ip>/admin/api/v1/...`` — the SAME one ATEM Setup drives (ATEM Setup is
just a browser pointed at ``/admin/``). We use it to READ the switcher's device
info and SET its stored name, so operators don't need ATEM Setup. Unauthenticated
on the LAN (same trust model as the control protocol). Older ATEMs (pre-web-admin)
have no REST API — callers get ``None`` / ``(False, …)`` and degrade gracefully.

Only ever WRITE ``/setupBasic/name`` — the same API also exposes network/IP
endpoints we deliberately never call. ``/remoteAdmin`` is READ only: its
``enabled`` flag is ATEM Setup's "configure via USB and Ethernet" switch; while
it's off the switcher only accepts configuration over USB, so our Ethernet
writes fail — we surface that instead of a bare error. Stdlib ``urllib`` only
(no new dep); short timeout; never raises.
"""
import json
import logging
import urllib.error
import urllib.request

from atem_control.netutil import is_valid_ip

logger = logging.getLogger(__name__)

_TIMEOUT = 4.0
_BASE = 'http://{ip}/admin/api/v1'


def get_device_info(ip):
    """Return ``{deviceName, productName, shortProductName, software, hostname}``
    for the ATEM at ``ip``, or ``None`` if the IP is invalid, the ATEM is
    unreachable, or it has no REST API (older model).

    A 5xx from the API path is a third case — the web setup is THERE but its
    API is failing (seen live: HTTP 500 on every endpoint of a switcher whose
    name was never set). That returns the same dict with empty fields and
    ``error`` set, so callers can say so instead of "no web setup"."""
    if not is_valid_ip(ip):
        return None
    url = _BASE.format(ip=ip) + '/setupBasic'
    try:
        req = urllib.request.Request(
            url, method='GET', headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read()).get('response', {})
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            logger.info("ATEM %s: web setup present but its API fails: HTTP %s", ip, e.code)
            return {'deviceName': '', 'productName': '', 'shortProductName': '',
                    'software': '', 'hostname': '', 'error': f'HTTP {e.code}'}
        logger.info("ATEM %s has no reachable REST config API: %s", ip, e)
        return None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        # No web admin on this model, or unreachable — not an error worth a card.
        logger.info("ATEM %s has no reachable REST config API: %s", ip, e)
        return None
    return {
        'deviceName': data.get('deviceName', ''),
        'productName': data.get('productName', ''),
        'shortProductName': data.get('shortProductName', ''),
        'software': data.get('software', ''),
        'hostname': data.get('hostname', ''),
    }


def get_remote_admin(ip):
    """Return ``{'enabled': bool}`` from the switcher's ``/remoteAdmin`` (ATEM
    Setup's "configure via USB and Ethernet" switch), or ``None`` if the IP is
    invalid, the ATEM is unreachable, or it has no REST API. Read-only — we
    never PUT this endpoint."""
    if not is_valid_ip(ip):
        return None
    url = _BASE.format(ip=ip) + '/remoteAdmin'
    try:
        req = urllib.request.Request(
            url, method='GET', headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read()).get('response', {})
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        logger.info("ATEM %s: no /remoteAdmin reply: %s", ip, e)
        return None
    return {'enabled': bool(data.get('enabled', True))}


def set_device_name(ip, name):
    """PUT a new device name to the ATEM. Returns ``(ok, error)``. Never raises."""
    if not is_valid_ip(ip):
        return (False, 'invalid ip')
    url = _BASE.format(ip=ip) + '/setupBasic/name'
    payload = json.dumps({'name': name}).encode('utf-8')
    try:
        req = urllib.request.Request(
            url, data=payload, method='PUT',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = getattr(resp, 'status', None) or resp.getcode()
    except urllib.error.HTTPError as e:
        return (False, f'HTTP {e.code}')
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return (False, str(e))
    if status and status >= 400:
        return (False, f'HTTP {status}')
    return (True, '')
