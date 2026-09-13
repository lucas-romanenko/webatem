import json
"""HTTP views for the ATEM control feature.

Endpoints:
    GET /atem/                   — Connect landing page
    GET /atem/control/           — Control surface
    GET /atem/api/status/        — Pool status JSON
    GET /atem/api/lookup-name/   — Saved-switcher name resolver for an IP
"""

import logging

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from atemwire._state import build_full_state
from atemwire.pool import ATEMInstanceManager

from atem_control import discovery, hooks
from atem_control.control.device_api import set_device_name
from atem_control.hooks import access_required
from atem_control.netutil import is_valid_ip
from atem_control.activity import ActivityLog, record_activity

logger = logging.getLogger(__name__)


def _page_context(request):
    """What both pages get from the host: the recent list, the switcher
    suggestions, deck names, whether discovery is on, anything extra."""
    h = hooks.get()
    return {
        'recent_atems': h.recent_atems(request, limit=5),
        'atem_equipment': h.switchers(),
        'hyperdeck_names': h.hyperdeck_names(),
        'discovery_enabled': h.discovery_enabled(),
        'server_settings_enabled': h.server_settings_enabled(),
        **h.template_context(request),
    }


@access_required
def atem_connect(request):
    """ATEM connection landing page."""
    return render(request, 'connect.html', {
        'initial_ip': request.GET.get('ip', ''),
        **_page_context(request),
    })


@access_required
def atem_control(request):
    """ATEM control interface."""
    return render(request, 'control.html', {
        'initial_ip': request.GET.get('ip', '192.168.1.100'),
        **_page_context(request),
    })


@access_required
def atem_status(request):
    """Check ATEM instance statuses. Optionally filter by ?ip= for a single instance."""
    try:
        instances = ATEMInstanceManager.list_instances()

        response_data = {
            'total_instances': len(instances),
            'instances': {}
        }

        for ip, instance_info in instances.items():
            response_data['instances'][ip] = {
                'connected': instance_info['connected'],
                'status': 'Connected' if instance_info['connected'] else 'Disconnected'
            }

            if instance_info['connected']:
                try:
                    # Ref-count the pool entry while we read state so a
                    # concurrent release can't tear the socket down under us.
                    # Identity-guarded release (SH-7): if OUR entry is evicted
                    # (worker death) and replaced while we read, a bare
                    # release_instance(ip) would steal a reference from the
                    # fresh entry's live holders.
                    instance = ATEMInstanceManager.get_instance(ip)
                    try:
                        state = build_full_state(instance['connection'])
                    finally:
                        ATEMInstanceManager.release_instance(ip, instance=instance)

                    if state.get('is_connected'):
                        response_data['instances'][ip].update({
                            'program': state.get('program', 0),
                            'preview': state.get('preview', 0),
                            'transition_active': state.get('transition', {}).get('in_transition', False),
                            'ftb_active': state.get('ftb', {}).get('active', False),
                            'ftb_disabled': state.get('ftb', {}).get('disabled', False)
                        })

                except Exception as e:
                    logger.warning(f"Could not get state for {ip}: {e}")

        requested_ip = request.GET.get('ip')
        if requested_ip and requested_ip in instances:
            return JsonResponse({
                'connected': instances[requested_ip]['connected'],
                'status': 'Connected' if instances[requested_ip]['connected'] else 'Disconnected',
                'ip_address': requested_ip,
                **response_data['instances'][requested_ip]
            })

        return JsonResponse(response_data)

    except Exception as e:
        logger.error(f"Error getting ATEM status: {e}")
        return JsonResponse({
            'connected': False,
            'status': 'Error',
            'error': str(e),
            'total_instances': 0,
            'instances': {}
        }, status=500)


@access_required
def atem_lookup_name(request):
    """The switcher's friendly name for an IP, from the host (an inventory,
    or what discovery has seen)."""
    ip_address = request.GET.get('ip', '').strip()
    if not ip_address:
        return JsonResponse({'found': False, 'name': None})
    name = hooks.get().name_for_ip(ip_address)
    if name:
        return JsonResponse({'found': True, 'name': name})
    return JsonResponse({'found': False, 'name': None})


_DISCOVERY_OFF = {'available': False, 'atems': [], 'subnet': None, 'disabled': True}


@access_required
@require_GET
def atem_discovered(request):
    """ATEMs seen on the network via passive mDNS/Bonjour listening.

    Lazily starts the mDNS browser on first call; the registry fills over
    the next second or two as announcements arrive, so the Connect page
    polls this. Sends nothing to any switcher.
    """
    if not hooks.get().discovery_enabled():
        return JsonResponse(_DISCOVERY_OFF)
    available = discovery.ensure_mdns_started()
    return JsonResponse({
        'available': available,
        'atems': discovery.discovered_atems(),
        # This host's own /24 — the page auto-sweeps it (plus any subnet
        # mDNS spots an ATEM on) so discovery works even where multicast
        # is blocked.
        'subnet': discovery.local_subnet(),
    })


@access_required
@require_GET
def atem_scan(request):
    """On-demand light subnet sweep for ATEMs that don't advertise over
    mDNS. Optional ``?subnet=192.168.1`` overrides the host's own /24.
    Half-open handshakes only — no session is established on any switcher.
    """
    if not hooks.get().discovery_enabled():
        return JsonResponse({'atems': [], 'disabled': True})
    subnet = request.GET.get('subnet', '').strip() or None
    result = discovery.scan_atems(subnet)
    return JsonResponse(result)



# =============================================================================
# Switcher Name (Settings > Switcher Name on the control page, _atem_info.html)
# — the ATEM's OWN stored name, read and set over its REST config API
# (control/device_api.py). The panel is synced from upstream, where a second
# name (the equipment list's) is offered too; there is no inventory here, so
# ``equipmentName`` is always empty and that affordance stays hidden.
# =============================================================================

_MAX_DEVICE_NAME = 32


def _clean_device_name(raw):
    """Strip control chars, collapse to a tidy single-line name, cap the length."""
    name = ''.join(ch for ch in str(raw or '') if ch.isprintable())
    return name.strip()[:_MAX_DEVICE_NAME].strip()


@access_required
@require_GET
def atem_device_info(request):
    """Read-only device info for the Switcher Name section: the ATEM's REST
    config (name / model / software). ``supported`` is False on older ATEMs
    with no web admin (the section then shows the name read-only)."""
    ip = (request.GET.get('ip') or '').strip()
    if not is_valid_ip(ip):
        return HttpResponseBadRequest('valid ip required')
    h = hooks.get()
    info = h.probe_device(ip)   # None if no REST API / unreachable
    return JsonResponse({
        'supported': info is not None,
        'apiError': (info or {}).get('error', ''),
        'deviceName': (info or {}).get('deviceName', ''),
        'productName': (info or {}).get('productName', ''),
        'software': (info or {}).get('software', ''),
        'hostname': (info or {}).get('hostname', ''),
        'ip': ip,
        # the host's own name for it, so the section can offer "use that one"
        'equipmentName': h.name_for_ip(ip) or '',
    })


@access_required
@require_POST
def atem_set_device_name(request):
    """Set the ATEM's stored device name (the ATEM Setup name) via its REST API."""
    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return HttpResponseBadRequest('invalid json')
    ip = (body.get('ip') or '').strip()
    name = _clean_device_name(body.get('name'))
    if not is_valid_ip(ip):
        return HttpResponseBadRequest('valid ip required')
    if not name:
        return HttpResponseBadRequest('name required')
    ok, err = set_device_name(ip, name)
    h = hooks.get()
    if ok:
        h.record_named(ip, name)          # a rename we made is never a question
    record_activity(
        feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
        action='set_device_name', user=getattr(request, 'user', None), target=ip,
        target_name=h.name_for_ip(ip) or '',
        summary=(f'Set switcher name to "{name}"' if ok else f'Failed to set switcher name: {err}'),
        success=ok, new_name=name,
    )
    if not ok:
        return JsonResponse({'success': False, 'error': err}, status=502)
    return JsonResponse({'success': True, 'deviceName': name})
