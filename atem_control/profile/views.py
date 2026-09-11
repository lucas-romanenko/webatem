"""HTTP views for the profile save/restore feature.

Endpoints:
    GET  /atem/profile/save_dialog_init/?ip=<ip>
    POST /atem/profile/save/?ip=<ip>     (body: {sections: {...}})
    POST /atem/profile/load_xml/         (multipart: profile=<xml file>)
    POST /atem/profile/load/             (multipart: ip + profile + sections + image_*)

Domain logic lives in ``profile/export.py`` (save) and ``atemwire.profile``
(load). The view functions here translate HTTP ↔ Python and render
responses.
"""

import json
import logging
import os
import tempfile

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST

from atemwire import ATEM, ApplyOptions, Profile
from atemwire.profile import SaveOptions

from atem_control.media_pool import watcher as _media_pool_service
from atem_control.profile.dialog import (
    describe_load_sections, describe_save_sections,
    me_options_from_sections, referenced_images, section_value,
)
from atem_control.profile.export import (
    ATEMConnectError, build_save_options, export_profile_zip,
    request_capture_cancel,
)
from atem_control.activity import ActivityLog
from atem_control.activity import record_activity
from atem_control.uploader import execute_upload
from atem_control.ip_upload_lock import hold_ip_upload_lock
from atem_control.netutil import is_valid_ip

logger = logging.getLogger(__name__)

# A profile XML is small (images are separate uploads); cap the read so a huge
# upload can't be slurped whole into memory and OOM the single ASGI worker.
_MAX_PROFILE_XML_BYTES = 16 * 1024 * 1024  # 16 MB


def _read_profile_upload(upload):
    """Read + decode an uploaded profile XML, capped. Returns ``(xml, None)``
    on success or ``(None, JsonResponse)`` the caller should return."""
    data = upload.read(_MAX_PROFILE_XML_BYTES + 1)
    if len(data) > _MAX_PROFILE_XML_BYTES:
        return None, JsonResponse(
            {'error': f'profile XML exceeds {_MAX_PROFILE_XML_BYTES} bytes'},
            status=413,
        )
    try:
        return data.decode('utf-8'), None
    except UnicodeDecodeError:
        return None, JsonResponse(
            {'error': 'profile must be UTF-8 XML'}, status=400)


def _build_apply_options(sections: dict) -> ApplyOptions:
    """Translate load-side section-selection JSON → ApplyOptions.

    Per-M/E cells arrive me-namespaced (``me<N>_program`` …
    ``me<N>_usk_<k>``) — one set per M/E tab the load descriptor made
    available (switcher has the M/E AND the XML contains its block) —
    and map into ``options.mes[N]`` via
    ``dialog.me_options_from_sections`` (the one place the namespacing
    is parsed, shared with the save mapping). M/Es with no submitted
    cells are fully deselected via ``me_options``, so blocks for them
    in the profile are left untouched. The apply path honors each cell
    independently (e.g., ``me0_usk_2: False`` skips that key entirely
    while leaving 1, 3, 4 to apply).

    Program/Preview default to ``False`` — the cuts-to-air gate.
    """
    return ApplyOptions(
        mes=me_options_from_sections(
            sections, program_default=False, preview_default=False),
        # Switcher-global.
        restore_downstream_keys=section_value(sections, 'downstream_keys', True),
        restore_color_generators=section_value(sections,
                                               'color_generators', True),
        restore_audio=section_value(sections, 'audio_mixer', True),
        # Other coarse fields.
        restore_aux=section_value(sections, 'auxiliaries', True),
        restore_video_mode=section_value(sections, 'video_mode', True),
        restore_inputs=section_value(sections, 'inputs', True),
        restore_macros=section_value(sections, 'macros', True),
        restore_media_players=section_value(sections, 'media_players', True),
        restore_media_pool_images=section_value(sections,
                                                'media_pool_images', True),
        restore_settings_flags=section_value(sections, 'settings_flags', True),
        restore_hyperdecks=section_value(sections, 'hyperdecks', True),
    )


def profile_save_dialog_init(request):
    """Return the section descriptor the save dialog renders.

    Query: ?ip=<ip>
    """
    ip = (request.GET.get('ip') or '').strip()
    if not is_valid_ip(ip):
        return JsonResponse({'error': 'a valid ip query param is required'}, status=400)
    try:
        with ATEM(ip) as atem:
            if not atem.connected:
                return JsonResponse(
                    {'error': f'failed to connect to ATEM at {ip}'},
                    status=503,
                )
            descriptor = describe_save_sections(atem)
            descriptor['atem_name'] = atem.product_name or 'ATEM'
            descriptor['ip'] = ip
            return JsonResponse(descriptor)
    except Exception as e:  # noqa: BLE001
        logger.exception("save_dialog_init failed for %s", ip)
        return JsonResponse({'error': f'{type(e).__name__}: {e}'}, status=500)


def profile_save(request):
    """Build the profile and deliver as a single download.

    GET  /profile/save?ip=<ip>     — returns full XML, no media (back-compat
                                     with the original curl-style callers).
    POST /profile/save?ip=<ip>     — body: {sections: {...}}.
                                     If media_pool_images is selected AND
                                     there are populated slots to capture,
                                     returns a ZIP (<basename>.xml at root +
                                     ATEM Media Pool/<name>.png entries —
                                     the same on-disk layout Software
                                     Control produces, just packed into a
                                     single transport). Otherwise returns
                                     the XML inline.

                                     During media-pool capture (5–10 s per
                                     slot), per-slot progress is fanned
                                     out over the per-IP Channels group as
                                     ``profile.capture_progress`` so the
                                     save dialog can render a progress bar
                                     and the operator can cancel at slot
                                     boundaries via /profile/save/cancel/.
                                     The session id linking the two is
                                     supplied by the frontend in the
                                     ``X-Capture-Session`` header.
    """
    ip = (request.GET.get('ip') or '').strip()
    if not is_valid_ip(ip):
        return JsonResponse({'error': 'a valid ip query param is required'}, status=400)

    sections: dict = {}
    legacy_get = False
    if request.method == 'POST':
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            return JsonResponse({'error': 'malformed JSON body'}, status=400)
        sections = body.get('sections', {}) if isinstance(body, dict) else {}
    else:
        legacy_get = True

    save_opts = build_save_options(sections) if sections else SaveOptions()

    # Frontend-supplied session id — lets progress messages identify
    # themselves so a stale dialog from a previous save click ignores
    # events from a newer one. Empty string when the legacy GET path
    # is in play; progress is a no-op in that case.
    capture_session = (request.headers.get('X-Capture-Session') or '').strip()

    channel_layer = get_channel_layer()
    group = _media_pool_service.group_name(ip)

    def progress(slot: int, completed: int, total: int,
                 slot_name: str, from_cache: bool) -> None:
        if channel_layer is None:
            return
        try:
            async_to_sync(channel_layer.group_send)(group, {
                'type': 'profile.capture_progress',
                'payload': {
                    'session_id': capture_session,
                    'ip': ip,
                    'slot': int(slot),
                    'slot_name': slot_name,
                    'completed': int(completed),
                    'total': int(total),
                    'from_cache': bool(from_cache),
                },
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("profile_save progress push failed: %s", exc)

    try:
        payload, content_type, filename = export_profile_zip(
            ip, save_opts,
            legacy_get=legacy_get,
            progress_callback=progress,
            cancel_session=capture_session,
        )
    except ATEMConnectError as e:
        return JsonResponse({'error': str(e)}, status=503)
    except Exception as e:  # noqa: BLE001
        logger.exception("profile_save failed for %s", ip)
        return JsonResponse({'error': f'{type(e).__name__}: {e}'}, status=500)

    record_activity(
        feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
        action='profile_save',
        target=ip,
        summary=f"Saved switcher profile from {ip} ({filename})",
        filename=filename,
    )

    response = HttpResponse(payload, content_type=content_type)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@require_POST
def profile_save_cancel(request):
    """Cancel an in-flight profile save's media-pool capture phase
    (2026-07-07). The save runs in a separate long-lived POST; this marks
    its session (X-Capture-Session, echoed in the JSON body as
    ``session_id``) for cancellation so the capture loop stops at the next
    slot boundary. Advertised in the dialog since forever but never wired —
    multi-minute captures were unabortable."""
    try:
        body = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        body = {}
    session_id = (body.get('session_id')
                  or request.headers.get('X-Capture-Session') or '').strip()
    if not session_id:
        return JsonResponse({'error': 'session_id required'}, status=400)
    request_capture_cancel(session_id)
    return JsonResponse({'ok': True, 'session_id': session_id})


@require_POST
def profile_load_xml(request):
    """Parse an uploaded profile XML and return the section descriptor +
    referenced images list.

    POST multipart/form-data:
        ip      : ATEM IP (required — M/E tab availability is gated by
                  the connected switcher's topology AND the file)
        profile : the .xml file (required)
    """
    ip = (request.POST.get('ip') or '').strip()
    if not is_valid_ip(ip):
        return JsonResponse({'error': 'a valid ip is required'}, status=400)

    upload = request.FILES.get('profile')
    if upload is None:
        return JsonResponse({'error': 'profile file is required'}, status=400)
    xml, err = _read_profile_upload(upload)
    if err is not None:
        return err

    try:
        profile = Profile.from_xml(xml)
    except Exception as e:
        return JsonResponse(
            {'error': f'XML parse failed: {type(e).__name__}: {e}'},
            status=400,
        )

    # Warm pool hit in the normal flow — the load dialog opens from the
    # control page, which holds a pooled session to the same ATEM.
    try:
        with ATEM(ip) as atem:
            if not atem.connected:
                return JsonResponse(
                    {'error': f'failed to connect to ATEM at {ip}'},
                    status=503,
                )
            descriptor = describe_load_sections(profile, atem)
    except Exception as e:  # noqa: BLE001
        logger.exception("profile_load_xml descriptor failed for %s", ip)
        return JsonResponse({'error': f'{type(e).__name__}: {e}'}, status=500)

    descriptor['xml'] = xml          # echo back so the second POST
    descriptor['xml_size'] = len(xml)  # carries the XML through
    return JsonResponse(descriptor)


@require_POST
def profile_load(request):
    """Apply a profile XML to a connected ATEM, optionally with images.

    POST multipart/form-data:
        ip       : ATEM IP (required)
        profile  : the XML file (required)
        sections : JSON-encoded section selection (optional; defaults
                   match the load-dialog defaults)
        image_<n>: zero or more uploaded image files; the form may
                   carry any number, named however the form layer
                   chose. Matched against XML <Still path="..."/>
                   filenames case-insensitively.

    Returns JSON:
        {applied: [...], skipped: [...], errors: [...],
         images: {uploaded: int, missing: [filenames], extra: [filenames],
                  upload_errors: [...]}}
    """
    ip = (request.POST.get('ip') or '').strip()
    if not is_valid_ip(ip):
        return JsonResponse({'error': 'a valid ip is required'}, status=400)

    upload = request.FILES.get('profile')
    if upload is None:
        return JsonResponse({'error': 'profile file is required'}, status=400)
    xml, err = _read_profile_upload(upload)
    if err is not None:
        return err

    try:
        profile = Profile.from_xml(xml)
    except Exception as e:
        logger.warning("profile_load parse failed: %s", e)
        return JsonResponse(
            {'error': f'XML parse failed: {type(e).__name__}: {e}'},
            status=400,
        )

    sections_raw = request.POST.get('sections') or '{}'
    try:
        sections = json.loads(sections_raw)
    except Exception:
        return JsonResponse({'error': 'malformed sections JSON'}, status=400)

    options = _build_apply_options(sections)

    # Apply XML config first.
    try:
        with ATEM(ip) as atem:
            if not atem.connected:
                return JsonResponse(
                    {'error': f'failed to connect to ATEM at {ip}'},
                    status=503,
                )
            result = profile.apply(atem, options)
    except Exception as e:  # noqa: BLE001
        logger.exception("profile_load apply failed for %s", ip)
        return JsonResponse({'error': f'{type(e).__name__}: {e}'}, status=500)

    image_report = {'uploaded': 0, 'missing': [],
                    'extra': [], 'upload_errors': []}

    # Image-upload pass — only if the operator selected media-pool images.
    if options.restore_media_pool_images:
        # Collect uploaded files keyed by lowercase basename.
        uploaded = {}
        for key in request.FILES:
            if key == 'profile':
                continue
            for fobj in request.FILES.getlist(key):
                fname = (fobj.name or '').strip()
                if not fname:
                    continue
                uploaded[fname.lower()] = fobj

        # Pull <Still> entries from XML.
        wanted = referenced_images(profile)

        # Match + materialize to a temp dir for the uploader.
        if wanted:
            tmpdir = tempfile.mkdtemp(prefix='atem-profile-load-')
            items = []
            try:
                for w in wanted:
                    fn = (w.get('filename') or '').strip()
                    if not fn:
                        continue
                    fobj = uploaded.pop(fn.lower(), None)
                    if fobj is None:
                        image_report['missing'].append(fn)
                        continue
                    path = os.path.join(tmpdir, fn)
                    with open(path, 'wb') as fp:
                        for chunk in fobj.chunks():
                            fp.write(chunk)
                    items.append((ip, w['slot'], path))

                # Anything left in `uploaded` was extra (operator picked
                # files not referenced by the XML). Reported but harmless.
                image_report['extra'] = list(uploaded.keys())

                if items:
                    try:
                        # Serialize against scheduled jobs + overlay presses on
                        # this ATEM: execute_upload opens the uploader's own
                        # aggressive-drain socket, and two of those to one
                        # switcher is the documented degradation mode (audit
                        # UP-1). All items here share this single IP.
                        with hold_ip_upload_lock(ip):
                            results = execute_upload(items, skip_tally=True)
                        for r in results:
                            if r.success:
                                image_report['uploaded'] += 1
                            else:
                                image_report['upload_errors'].append(
                                    f"slot {r.slot}: {r.error}")
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "profile_load image upload failed for %s", ip)
                        image_report['upload_errors'].append(
                            f"{type(exc).__name__}: {exc}")
            finally:
                # Clean up temp files
                try:
                    for f in os.listdir(tmpdir):
                        os.unlink(os.path.join(tmpdir, f))
                    os.rmdir(tmpdir)
                except Exception:  # noqa: BLE001
                    pass

    record_activity(
        feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
        action='profile_load',
        target=ip,
        success=not result.errors,
        summary=(f"Restored switcher profile to {ip}"
                 + (f" ({image_report['uploaded']} image(s))"
                    if image_report.get('uploaded') else "")),
        applied=len(result.applied or []), skipped=len(result.skipped or []),
        errors=len(result.errors or []),
        images_uploaded=image_report.get('uploaded', 0),
    )

    return JsonResponse({
        'applied': result.applied,
        'skipped': result.skipped,
        'errors': result.errors,
        'summary': result.summary(),
        'product': profile.product,
        'video_mode': profile.video_mode,
        'images': image_report,
    })
