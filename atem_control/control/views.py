"""HTTP views for the ATEM control feature.

Endpoints:
    GET /atem/                   — Connect landing page
    GET /atem/control/           — Control surface
    GET /atem/api/status/        — Pool status JSON
    GET /atem/api/lookup-name/   — Saved-switcher name resolver for an IP
"""

import logging

from django.http import JsonResponse
from django.shortcuts import render

from pyatem._state import build_full_state
from pyatem.pool import ATEMInstanceManager

from atem_control.models import get_recent_atems

logger = logging.getLogger(__name__)


def atem_connect(request):
    """ATEM connection landing page."""
    return render(request, 'connect.html', {
        'initial_ip': request.GET.get('ip', ''),
        'recent_atems': get_recent_atems(limit=5),
    })


def atem_control(request):
    """ATEM control interface."""
    return render(request, 'control.html', {
        'initial_ip': request.GET.get('ip', '192.168.1.100'),
        'recent_atems': get_recent_atems(limit=5),
    })


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
                    instance = ATEMInstanceManager.get_instance(ip)
                    try:
                        state = build_full_state(instance['connection'])
                    finally:
                        ATEMInstanceManager.release_instance(ip)

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


def atem_lookup_name(request):
    """Lookup equipment name by IP address"""
    ip_address = request.GET.get('ip', '').strip()

    if not ip_address:
        return JsonResponse({
            'found': False,
            'name': None
        })

    # No name database in this build — the page shows the bare IP.
    return JsonResponse({'found': False, 'name': None})

