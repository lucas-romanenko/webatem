"""The media pool's stills at full size, on disk, by content hash.

A switcher stamps every still slot with an MD5 of its pixels (the MPfe
hash). The picture behind a hash never changes, so a PNG kept under it is
the slot's picture for as long as the slot reports that hash. A profile
save takes a still from here instead of downloading it (about 1.8 s for a
1080p still, which is the switcher's own pace, not ours to speed up), and
the watcher makes a slot's preview from it without a transfer at all.
ATEM Software Control works the same way: it downloads every still when it
connects, and its save writes those (a capture of it, 2026-10-14).

Keyed by the hash alone, not by the switcher, so an image on several
switchers is stored, and downloaded, once. The hash is the one the
uploader stamped on the slot; an empty or all-zero hash is never stored or
matched.

Bounded, because a host may serve many switchers: ``Hooks.still_cache_dir``
says where (None keeps nothing), ``still_cache_max_bytes`` how much (the
least recently used still goes first), ``still_cache_max_age_days`` how
long a still nobody has shown or saved stays. A miss costs a download, as
it did before this existed, so evicting is always safe.

On disk rather than in memory, so it survives a restart and every process
of a host shares it: an uploader in a process of its own seeds it, the
server reads it. A write is a temporary file renamed into place; a read
that loses a race with an eviction is a miss.
"""
import io
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HASH = re.compile(r'[0-9a-f]{32}')
_ENTRY = re.compile(r'[0-9a-f]{32}\.png')
_TEMP = re.compile(r'\.[0-9a-f]{32}\.[0-9]+\.[0-9]+\.tmp')
_ZERO = '0' * 32

# A frame is kept as a PNG at zlib level 1: about 0.3 s to write a 1080p
# still against 1.2 s at Pillow's default, for a file about 10% larger.
PNG_COMPRESS_LEVEL = 1

# A temporary file older than this was left by a process that died
# mid-write; a sweep removes it.
_STALE_TEMP_SECONDS = 3600

_sweep_lock = threading.Lock()
_writer: Optional[ThreadPoolExecutor] = None
_writer_lock = threading.Lock()


def _settings():
    """(directory or None, max bytes, max age in seconds or None), as the
    host says."""
    from atem_control import hooks
    h = hooks.get()
    directory = h.still_cache_dir()
    days = h.still_cache_max_age_days()
    return (Path(directory) if directory else None,
            int(h.still_cache_max_bytes()),
            float(days) * 86400 if days else None)


def _path(hash_hex, cfg=None) -> Optional[Path]:
    """The file a hash is kept in, or None: no cache, or not a real hash.
    ``cfg`` is a ``_settings()`` taken earlier (a queued write keeps the
    place it was queued for)."""
    h = (hash_hex or '').lower()
    if not _HASH.fullmatch(h) or h == _ZERO:
        return None
    directory = (cfg or _settings())[0]
    return directory / f'{h}.png' if directory else None


def get(hash_hex) -> Optional[bytes]:
    """The PNG kept for this hash, or None. A hit counts as a use."""
    path = _path(hash_hex)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    _touch(path)
    return data or None


def contains(hash_hex) -> bool:
    """Is a PNG kept for this hash? (Not a use.)"""
    path = _path(hash_hex)
    return path is not None and path.is_file()


def touch(hash_hex) -> None:
    """A switcher shows this still: it is in use, keep it."""
    path = _path(hash_hex)
    if path is not None:
        _touch(path)


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


def put(hash_hex, png: bytes, cfg=None) -> None:
    """Keep ``png`` as the picture for this hash, then sweep."""
    cfg = cfg or _settings()
    path = _path(hash_hex, cfg)
    if path is None or not png:
        return
    temp = path.with_name(f'.{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(png)
        os.replace(temp, path)
    except OSError as e:
        logger.warning("still cache: could not keep %s: %s", path.name, e)
        _unlink(temp)
        return
    sweep(cfg)


def png_from_rgba(rgba: bytes, width: int, height: int) -> bytes:
    """A frame as atemwire's ``atem_to_rgb`` returns it (RGBA8888), as the
    PNG the cache keeps."""
    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes('RGBA', (width, height), rgba).save(
        buf, format='PNG', compress_level=PNG_COMPRESS_LEVEL)
    return buf.getvalue()


def put_frame(hash_hex, rgba: bytes, width: int, height: int, cfg=None) -> None:
    """Keep an RGBA frame as this hash's PNG, unless one is kept already."""
    cfg = cfg or _settings()
    path = _path(hash_hex, cfg)
    if path is None or path.is_file():
        return
    try:
        png = png_from_rgba(rgba, width, height)
    except Exception as e:  # noqa: BLE001
        logger.warning("still cache: could not encode %s: %s", hash_hex, e)
        return
    put(hash_hex, png, cfg)


def put_frame_later(hash_hex, rgba: bytes, width: int, height: int) -> None:
    """``put_frame`` on the cache's own thread, so a caller that downloads
    (the watcher) goes straight on to the next still. A process lets the
    thread finish before it exits (concurrent.futures joins it), so an
    uploader in a process of its own still leaves its seed behind."""
    global _writer
    cfg = _settings()
    if _path(hash_hex, cfg) is None:
        return
    with _writer_lock:
        if _writer is None:
            _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='still-cache')
        writer = _writer
    writer.submit(put_frame, hash_hex, rgba, width, height, cfg)


def wait_for_writes() -> None:
    """Let every ``put_frame_later`` queued so far finish (tests, shutdown)."""
    with _writer_lock:
        writer = _writer
    if writer is not None:
        writer.submit(lambda: None).result()


def sweep(cfg=None) -> None:
    """Delete the stills past the age limit, then the least recently used
    until the cache is within its size, and any temporary file a dead
    process left. Only the cache's own files are ever touched."""
    directory, max_bytes, max_age = cfg or _settings()
    if directory is None:
        return
    with _sweep_lock:
        now = time.time()
        try:
            found = list(os.scandir(directory))
        except OSError:
            return
        kept = []
        for entry in found:
            try:
                st = entry.stat()
            except OSError:
                continue
            if _TEMP.fullmatch(entry.name):
                if now - st.st_mtime > _STALE_TEMP_SECONDS:
                    _unlink(entry.path)
            elif _ENTRY.fullmatch(entry.name):
                if max_age is not None and now - st.st_mtime > max_age:
                    _unlink(entry.path)
                else:
                    kept.append((st.st_mtime, st.st_size, entry.path))
        total = sum(size for _, size, _ in kept)
        for _, size, path in sorted(kept):
            if total <= max_bytes:
                break
            _unlink(path)
            total -= size


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
