"""Media-pool drag-drop upload.

One image, one slot, one ATEM. The view validates and stores the file, then
hands it to the host (``hooks.upload_still``). Standalone WebATEM's default
runs the upload on a background thread in THIS process via
``atem_control.uploader.execute_upload`` (its own short-lived atemwire socket
with ``aggressive_drain``), so the watcher lockout and the post-upload
thumbnail refresh are direct in-process calls; a hosting platform queues it.

Response contract (relied on by ``atem_control.js`` ``uploadSlot``):
``{'success': bool, 'error': str?}``. While the upload runs, the switcher's
group hears ``mediapool.upload_progress`` as it really moves (the slot's ring
shows that and nothing else), and then ``mediapool.slot_uploaded`` or
``mediapool.upload_failed`` once it has ended.
"""
import logging
import os
import shutil
import threading
import time

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from atem_control import hooks
from atem_control.hooks import access_required
from atem_control.media_pool import watcher as media_pool_service
from atem_control.storage import UPLOADS_DIR, ensure_dir

logger = logging.getLogger(__name__)



def _make_job_dir():
    timestamp = str(time.time())
    job_dir = ensure_dir(UPLOADS_DIR / timestamp)
    return timestamp, str(job_dir)


def start_in_process_upload(ip, slot, file_path, job_dir):
    """The default ``hooks.upload_still``: lock out the watcher's downloads
    for this ATEM BEFORE any upload traffic starts, then push on a
    background thread."""
    media_pool_service.note_upload_started(ip, slot)
    threading.Thread(
        target=_run_upload, args=(ip, slot, file_path, job_dir),
        name=f"dragdrop-upload-{ip}-{slot}", daemon=True,
    ).start()


class _ProgressRelay:
    """``execute_upload``'s ``progress`` for one drag-and-drop upload: tells
    the switcher's group what the upload is doing (``mediapool.upload_progress``,
    ``{slot, stage, percent}``) each time it really moves, at most once per
    whole percent (a hundred messages a still). The ring used to ease towards
    90 % on a timer whatever the switcher did, so a stalled upload looked like
    one about to finish."""

    def __init__(self, ip, send):
        self.ip = ip
        self.send = send
        self.last = {}           # slot -> (stage, percent) last sent

    def __call__(self, ip, slot, stage, percent):
        pct = None if percent is None else int(percent)
        prev = self.last.get(slot)
        if prev is not None and prev[0] == stage and (pct is None or pct <= (prev[1] or 0)):
            return                       # nothing new to say
        self.last[slot] = (stage, pct)
        self.send(self.ip, {'type': 'mediapool.upload_progress',
                            'payload': {'slot': slot, 'stage': stage, 'percent': pct}})


def _run_upload(ip, slot, file_path, job_dir):
    """Background-thread body: push the still, then release the watcher
    lockout (which re-queues the slot's thumbnail download) and clean up."""
    from atem_control.uploader import execute_upload
    error = 'the upload crashed on the server'
    # Unknown until the uploader says: a crash may come before or after the
    # switcher took the still.
    changed = None
    try:
        results = execute_upload([(ip, slot, file_path)], skip_tally=True,
                                 progress=_ProgressRelay(ip, _group_send))
        failed = [r for r in results if not r.success]
        if failed:
            error = '; '.join(str(r.error or 'failed') for r in failed)
            changed = None if any(r.transfer_complete for r in failed) else False
            logger.error(f"Drag-drop upload to {ip} slot {slot} failed: {error}")
        else:
            error = None
            logger.info(f"Drag-drop upload to {ip} slot {slot} complete")
    except Exception:
        logger.exception(f"Drag-drop upload to {ip} slot {slot} crashed")
    finally:
        _tell_browsers(ip, slot, error, changed)
        try:
            media_pool_service.note_upload_finished(ip, slot)
        except Exception:
            logger.exception("note_upload_finished failed")
        shutil.rmtree(job_dir, ignore_errors=True)


def _group_send(ip, message):
    """Send ``message`` to the pages showing this switcher. Never raises."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from atem_control.media_pool.watcher import group_name
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(group_name(ip), message)
    except Exception:
        logger.exception(f"telling the browsers {message.get('type')} on {ip} failed")


def _tell_browsers(ip, slot, error, changed=False):
    """Say how a drag-and-drop upload ended to the pages showing this
    switcher, which otherwise learn it only from the watcher's new thumbnail
    (one more transfer over the same link) and never of a failure: the ring
    held at its 90 % estimate until a two-minute safety timer cleared it
    without a word (#63).

    A failure carries the uploader's ``reason`` and ``changed``: False when
    the switcher never took the whole still (its slot keeps the old one),
    None when that is not known (the still went over and the check after it
    failed, or the upload crashed). ``status`` is the older wording a host's
    own message also uses."""
    if error is None:
        message = {'type': 'mediapool.slot_uploaded', 'payload': {'slot': slot}}
    else:
        message = {'type': 'mediapool.upload_failed',
                   'payload': {'slot': slot, 'status': f'failed ({error})',
                               'reason': error, 'changed': changed}}
    _group_send(ip, message)


@access_required
@require_POST
def media_pool_upload(request):
    ip = (request.POST.get('ip') or '').strip()
    if not ip:
        return JsonResponse({'success': False, 'error': 'Missing IP address'}, status=400)

    try:
        slot = int(request.POST.get('slot'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid slot'}, status=400)
    if not (0 <= slot < 32):
        return JsonResponse({'success': False, 'error': 'Slot must be 0-31'}, status=400)

    if 'image' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No image provided'}, status=400)

    upload_file = request.FILES['image']
    file_name = str(upload_file).replace(' ', '_')

    timestamp, job_dir = _make_job_dir()
    file_path = os.path.join(job_dir, file_name)

    try:
        with open(file_path, 'wb+') as destination:
            for chunk in upload_file.chunks():
                destination.write(chunk)

        ok, err = hooks.get().validate_still(ip, file_path, file_name)
        if not ok:
            shutil.rmtree(job_dir, ignore_errors=True)
            return JsonResponse({'success': False, 'error': err}, status=400)

        logger.info(f"Drag-drop upload queued: ip={ip} slot={slot} file={file_name}")

        ok, err = hooks.get().upload_still(ip, slot, file_path, job_dir, user=getattr(request, 'user', None))
        if not ok:
            shutil.rmtree(job_dir, ignore_errors=True)
            return JsonResponse({'success': False, 'error': err or 'Upload refused'}, status=400)
        return JsonResponse({'success': True, 'slot': slot})

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception(f"media_pool_upload error: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
