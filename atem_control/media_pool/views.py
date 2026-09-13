"""Media-pool drag-drop upload.

One image, one slot, one ATEM. The view validates and stores the file, then
hands it to the host (``hooks.upload_still``). Standalone WebATEM's default
runs the upload on a background thread in THIS process via
``atem_control.uploader.execute_upload`` (its own short-lived atemwire socket
with ``aggressive_drain``), so the watcher lockout and the post-upload
thumbnail refresh are direct in-process calls; a hosting platform queues it.

Response contract (relied on by ``atem_control.js`` ``uploadSlot``):
``{'success': bool, 'error': str?}``. The slot tile clears its "uploading"
state when the watcher re-downloads the slot and broadcasts the new thumb.
"""
import logging
import os
import shutil
import threading
import time

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from PIL import Image

from atem_control import hooks
from atem_control.hooks import access_required
from atem_control.media_pool import watcher as media_pool_service
from atem_control.storage import UPLOADS_DIR, ensure_dir

logger = logging.getLogger(__name__)


def _make_job_dir():
    timestamp = str(time.time())
    job_dir = ensure_dir(UPLOADS_DIR / timestamp)
    return timestamp, str(job_dir)


def _is_16_9(width, height):
    return abs((width / height) - (16 / 9)) < 0.01


def validate_and_process_image(file_path, file_name):
    """Returns ``(ok, needs_resize, (width, height), error)``. Accepts
    exactly 1920x1080, or larger 16:9 (resized down by the caller)."""
    try:
        with Image.open(file_path) as image:
            width, height = image.size
            if width == 1920 and height == 1080:
                return True, False, (width, height), None
            if width < 1920 or height < 1080:
                return False, False, (width, height), (
                    f"Image {file_name} is too small ({width}x{height}) "
                    f"and cannot be resized."
                )
            if not _is_16_9(width, height):
                return False, False, (width, height), (
                    f"Image {file_name} must be 16:9 aspect ratio. "
                    f"Current ratio: {width / height:.3f}."
                )
            return True, True, (width, height), None
    except Exception as e:
        return False, False, (0, 0), f"Error processing image {file_name}: {e}"


def validate_and_resize_1080p(file_path, file_name):
    """The default ``hooks.validate_still``: 1920x1080 exactly, or a larger
    16:9 image resized down to it, in place. Returns (ok, error)."""
    ok, needs_resize, original_dims, err = validate_and_process_image(file_path, file_name)
    if not ok:
        return False, err
    if needs_resize:
        resized, resize_msg = resize_image_to_1920x1080(file_path)
        if not resized:
            return False, resize_msg
        logger.info(f"Resized {file_name} from {original_dims[0]}x{original_dims[1]}")
    return True, None


def resize_image_to_1920x1080(file_path):
    """Resize a validated oversized 16:9 image to exactly 1920x1080."""
    try:
        with Image.open(file_path) as image:
            image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            image.save(file_path, 'JPEG', quality=95, optimize=True)
        return True, "Resized to 1920x1080"
    except Exception as e:
        logger.error(f"Resize failed: {e}")
        return False, f"Resize failed: {e}"


def start_in_process_upload(ip, slot, file_path, job_dir):
    """The default ``hooks.upload_still``: lock out the watcher's downloads
    for this ATEM BEFORE any upload traffic starts, then push on a
    background thread."""
    media_pool_service.note_upload_started(ip, slot)
    threading.Thread(
        target=_run_upload, args=(ip, slot, file_path, job_dir),
        name=f"dragdrop-upload-{ip}-{slot}", daemon=True,
    ).start()


def _run_upload(ip, slot, file_path, job_dir):
    """Background-thread body: push the still, then release the watcher
    lockout (which re-queues the slot's thumbnail download) and clean up."""
    from atem_control.uploader import execute_upload
    try:
        results = execute_upload([(ip, slot, file_path)], skip_tally=True)
        failed = [r for r in results if not r.success]
        if failed:
            logger.error(
                f"Drag-drop upload to {ip} slot {slot} failed: "
                + '; '.join(str(getattr(r, 'message', r)) for r in failed)
            )
        else:
            logger.info(f"Drag-drop upload to {ip} slot {slot} complete")
    except Exception:
        logger.exception(f"Drag-drop upload to {ip} slot {slot} crashed")
    finally:
        try:
            media_pool_service.note_upload_finished(ip, slot)
        except Exception:
            logger.exception("note_upload_finished failed")
        shutil.rmtree(job_dir, ignore_errors=True)


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
