"""HTTP endpoints for HyperDeck transport control (clip browser + play/pause/
stop/loop + live status), called by the control-page HyperDeck modal.

Decks are addressed by IP (the deck's own 9993 address, as configured in the
HyperDecks settings panel / ATEM binding). Auth mirrors the ATEM control page:
authenticated + ``can_access_atem``.
"""
import json
import logging

from django.http import HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from atem_control.hyperdeck.connection import with_deck
from pyhyperdeck import HyperdeckError

logger = logging.getLogger(__name__)


class _BadAction(Exception):
    pass


def _deck_name(ip):
    # No name database in this build — the modal shows the deck's IP.
    return None


def _slot_label(sid, nslots, si):
    """Friendly bay label. The deck only names a slot once media is mounted
    (``sd1`` / ``ssd1`` / ``usb``), so empty bays get a default matching the
    hardware layout of the Studio line: SD1..SDn with the last bay as EXT
    (the USB-C "EXT DISK" port) on 3+-slot decks."""
    raw = si.get('slot name') or si.get('device') or ''
    if raw:
        return raw.upper()
    if nslots >= 3 and sid == nslots:
        return 'EXT'
    return 'SD%d' % sid


def _read_state(hd):
    """Full deck snapshot: model, slots (with connected status), every card's
    clips (tagged with their slot), and transport. Used on open / refresh —
    the ~1 Hz poll uses the lighter ``_read_status``."""
    info = hd.device_info()
    try:
        nslots = int(info.get('slot count') or 0)
    except (TypeError, ValueError):
        nslots = 0
    transport = hd.transport_info()
    slots, clips = [], []
    for sid in range(1, nslots + 1):
        try:
            si = hd.slot_info(sid)
        except HyperdeckError:
            continue
        status = (si.get('status') or 'unknown').lower()
        name = _slot_label(sid, nslots, si)
        slots.append({
            'id': sid, 'name': name, 'status': status,
            'connected': status == 'mounted',
            'volume': si.get('volume name') or '',
        })
        if status == 'mounted':
            try:
                for c in hd.disk_list(sid):
                    clips.append({
                        'clip_id': c.clip_id, 'name': c.name,
                        'duration': c.duration, 'slot': sid, 'slot_name': name,
                    })
            except HyperdeckError:
                pass
    return {
        'model': info.get('model') or '',
        'video_format': transport.get('video format') or '',
        'slots': slots,
        'clips': clips,
        'transport': transport,
    }


def _read_status(hd):
    """Just live transport status — the fast-changing part the modal polls."""
    return {'transport': hd.transport_info()}


@require_GET
def hyperdeck_state(request):
    """Full snapshot (slots + per-card clips + transport + name). Fetched on
    modal open / deck switch / manual refresh."""
    ip = (request.GET.get('ip') or '').strip()
    if not ip:
        return HttpResponseBadRequest('ip required')
    try:
        state = with_deck(ip, _read_state)
        state['name'] = _deck_name(ip)
        return JsonResponse(state)
    except (OSError, HyperdeckError) as e:
        return JsonResponse({'error': str(e), 'name': _deck_name(ip)}, status=502)


@require_GET
def hyperdeck_status(request):
    """Live transport status only — polled ~1 Hz while the modal is open."""
    ip = (request.GET.get('ip') or '').strip()
    if not ip:
        return HttpResponseBadRequest('ip required')
    try:
        return JsonResponse(with_deck(ip, _read_status))
    except (OSError, HyperdeckError) as e:
        return JsonResponse({'error': str(e)}, status=502)


@require_POST
def hyperdeck_transport(request):
    """Run one transport action on a deck and return the fresh status.

    Body: ``{ip, action, clip_id?, loop?, single_clip?}``;
    ``action`` ∈ play / pause / stop / goto.
    """
    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return HttpResponseBadRequest('invalid json')
    ip = (body.get('ip') or '').strip()
    action = body.get('action')
    if not ip or not action:
        return HttpResponseBadRequest('ip and action required')

    def run(hd):
        # Selecting a clip on a specific card means making that card active
        # first (clip ids are per-slot; the timeline follows the active slot).
        slot = body.get('slot')
        if slot is not None and action in ('play', 'goto'):
            hd.slot_select(int(slot))
        if action == 'play':
            hd.play(loop=bool(body.get('loop')),
                    single_clip=bool(body.get('single_clip')),
                    clip_id=body.get('clip_id'))
        elif action == 'pause':
            hd.pause()
        elif action == 'stop':
            hd.stop()
        elif action == 'goto':
            hd.goto_clip(int(body['clip_id']))
        else:
            raise _BadAction(action)
        return hd.transport_info()

    try:
        transport = with_deck(ip, run)
    except _BadAction as e:
        return HttpResponseBadRequest(f'unknown action: {e}')
    except (KeyError, ValueError, TypeError):
        return HttpResponseBadRequest('bad clip_id')
    except (OSError, HyperdeckError) as e:
        return JsonResponse({'error': str(e)}, status=502)

    # Activity log: a deck transport is an ATEM-Control-page action (the
    # modal lives there, matching ASC's HyperDeck palette) on a HyperDeck
    # device — the two-axis model's canonical cross-case.
    _log_transport(request, ip, action, body)

    return JsonResponse({'transport': transport})


_TRANSPORT_VERB = {'play': 'Played', 'pause': 'Paused', 'stop': 'Stopped',
                   'goto': 'Cued'}


def _log_transport(request, ip, action, body):
    try:
        from atem_control.activity import ActivityLog
        from atem_control.activity import record_activity
        name = _deck_name(ip) or ''
        label = name or ip
        verb = _TRANSPORT_VERB.get(action, action)
        clip = body.get('clip_id')
        extras = []
        if clip is not None:
            extras.append(f"clip {clip}")
        if action == 'play' and body.get('loop'):
            extras.append("loop")
        detail = (' (' + ', '.join(extras) + ')') if extras else ''
        record_activity(
            feature=ActivityLog.FEATURE_ATEM_CONTROL,
            device=ActivityLog.DEVICE_HYPERDECK,
            action=f'hyperdeck_{action}',
            target=ip, target_name=name,
            summary=f"{verb} HyperDeck {label}{detail}",
            clip_id=clip, loop=bool(body.get('loop')),
            slot=body.get('slot'))
    except Exception:
        logger.exception("hyperdeck transport activity log failed")
