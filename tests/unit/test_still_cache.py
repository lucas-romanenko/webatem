"""The media pool's still cache (media_pool/still_cache.py): full stills on
disk by the switcher's MD5, bounded by the host, and the three places that
use it — the watcher (keeps what it downloads, makes previews from what is
kept), the profile save (writes kept stills without a download) and the
uploader (seeds the picture it just sent).

conftest.py points the cache at a folder of each test's own.
"""
import hashlib
import io
import os
import time
import zipfile

import pytest
from PIL import Image

from atem_control import hooks, storage
from atem_control.media_pool import still_cache
from atem_control.media_pool import watcher as W

A = 'ab' * 16
B = 'cd' * 16
C = 'ef' * 16


class SmallCache(hooks.Hooks):
    """A host with its own bounds; the place stays the default (tmp). The
    test sets them on the live instance: pytest imports this module under
    another name than the setting does, so the class here isn't the one
    the hooks were made from."""
    max_bytes = 1 << 30
    max_age_days = 7

    def still_cache_max_bytes(self):
        return self.max_bytes

    def still_cache_max_age_days(self):
        return self.max_age_days


class NoCache(hooks.Hooks):
    def still_cache_dir(self):
        return None


@pytest.fixture
def small(settings):
    settings.WEBATEM_HOOKS = 'tests.unit.test_still_cache.SmallCache'
    hooks.reset()
    yield hooks.get()
    hooks.reset()


@pytest.fixture
def none(settings):
    settings.WEBATEM_HOOKS = 'tests.unit.test_still_cache.NoCache'
    hooks.reset()
    yield
    hooks.reset()


def _png(w=8, h=4, colour=(10, 20, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGBA', (w, h), colour).save(buf, format='PNG')
    return buf.getvalue()


def _frame(w=16, h=8):
    """A switcher frame (YCbCrA 4:2:2) of a small picture, and its MD5 hex."""
    from atemwire.imaging import rgb_to_atem
    img = Image.new('RGBA', (w, h), (200, 40, 30, 255))
    for x in range(w // 2):
        for y in range(h):
            img.putpixel((x, y), (30, 200, 120, 255))
    raw = rgb_to_atem(img.tobytes(), w, h, premultiply=False)
    return raw, hashlib.md5(raw).hexdigest()


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------

def test_a_still_is_kept_by_its_hash_and_shared_by_every_switcher():
    still_cache.put(A, b'png-a')
    assert still_cache.get(A) == b'png-a'
    assert still_cache.get(A.upper()) == b'png-a'                 # the switcher's hex, any case
    assert still_cache.contains(A) and not still_cache.contains(B)
    assert (storage.STILL_CACHE_DIR / f'{A}.png').read_bytes() == b'png-a'


def test_only_a_real_hash_is_kept():
    for bad in ('', None, '0' * 32, 'xyz', '../../etc/passwd', A + '/x', 'ab' * 15):
        still_cache.put(bad, b'x')
        assert still_cache.get(bad) is None and not still_cache.contains(bad)
    assert not storage.STILL_CACHE_DIR.exists() or list(storage.STILL_CACHE_DIR.iterdir()) == []


def test_a_host_can_keep_nothing(none):
    still_cache.put(A, b'x')
    still_cache.put_frame_later(A, b'\0' * 64, 4, 4)
    still_cache.wait_for_writes()
    assert still_cache.get(A) is None and not still_cache.contains(A)


def test_the_least_recently_used_go_first_past_the_cap(small):
    small.max_bytes = 25
    for i, h in enumerate((A, B, C)):
        still_cache.put(h, b'x' * 10)
        _age(storage.STILL_CACHE_DIR / f'{h}.png', 100 - i * 10)   # A oldest
    # 30 bytes > 25: A, the least recently used, went when C came in
    assert not still_cache.contains(A) and still_cache.contains(B) and still_cache.contains(C)
    assert still_cache.get(B) == b'x' * 10                         # a use: B is now the newest
    still_cache.put(A, b'x' * 10)
    assert still_cache.contains(B) and still_cache.contains(A) and not still_cache.contains(C)


def test_a_still_nobody_has_shown_for_the_age_limit_goes(small):
    still_cache.put(A, b'old')
    still_cache.put(B, b'new')
    _age(storage.STILL_CACHE_DIR / f'{A}.png', 8 * 86400)
    still_cache.sweep()
    assert not still_cache.contains(A) and still_cache.contains(B)
    # a switcher showing it counts as a use
    still_cache.put(A, b'old')
    _age(storage.STILL_CACHE_DIR / f'{A}.png', 6 * 86400)
    still_cache.touch(A)
    _age(storage.STILL_CACHE_DIR / f'{B}.png', 8 * 86400)
    small.max_age_days = None                                      # None: only the cap applies
    still_cache.sweep()
    assert still_cache.contains(A) and still_cache.contains(B)


def test_a_sweep_touches_only_the_cache_s_own_files():
    still_cache.put(A, b'x')
    d = storage.STILL_CACHE_DIR
    stale = d / f'.{B}.1.2.tmp'
    fresh = d / f'.{C}.1.2.tmp'
    other = d / 'notes.txt'
    for p in (stale, fresh, other):
        p.write_bytes(b'x')
    _age(stale, 2 * 3600)
    _age(other, 30 * 86400)
    still_cache.sweep()
    assert not stale.exists() and fresh.exists() and other.exists() and still_cache.contains(A)


def test_a_frame_is_kept_as_a_png_of_the_same_pixels():
    rgba = Image.new('RGBA', (16, 8), (1, 2, 3, 200)).tobytes()
    still_cache.put_frame_later(A, rgba, 16, 8)
    still_cache.wait_for_writes()
    img = Image.open(io.BytesIO(still_cache.get(A)))
    assert img.size == (16, 8) and img.convert('RGBA').tobytes() == rgba
    # a frame for a hash already kept is not encoded again
    still_cache.put(B, b'kept')
    still_cache.put_frame(B, rgba, 16, 8)
    assert still_cache.get(B) == b'kept'


# --------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------

class _NoDownloads:
    is_connected = is_ready = True
    mixerstate = {}

    def download_still(self, *a, **k):
        raise AssertionError('downloaded a still the cache keeps')


class _FakeMPfe:
    def __init__(self, index, hash_bytes, is_used=True, name=b'still'):
        self.index, self.hash, self.is_used, self.name = index, hash_bytes, is_used, name


def _watcher_with_slot(slot, hash_hex):
    w = W.MediaPoolWatcher('10.9.9.9')
    w._connected_flag = True
    w._slots[slot] = {'index': slot, 'isUsed': True, 'hash': hash_hex, 'fileName': 's', 'thumb': None}
    w._conn = _NoDownloads()
    return w


def test_a_kept_still_makes_the_preview_without_a_download():
    from atemwire.imaging import atem_to_rgb
    raw, h = _frame()
    rgba = atem_to_rgb(raw, 16, 8)
    still_cache.put(h, still_cache.png_from_rgba(rgba, 16, 8))
    W._thumb_cache.clear()
    w = _watcher_with_slot(3, h)
    assert w._download_task(3, h, 1) is False
    downloaded = 'data:image/jpeg;base64,' + __import__('base64').b64encode(
        W._encode_thumbnail_from_rgba(rgba, 16, 8)).decode('ascii')
    assert w._slots[3]['thumb'] == downloaded                     # the preview a download gives
    assert W._thumb_cache_get('10.9.9.9', h) == downloaded
    W._thumb_cache.clear()


def test_a_downloaded_still_is_kept_at_full_size():
    raw, h = _frame()
    w = _watcher_with_slot(1, h)
    w._do_decode(1, raw, h, 16, 8)
    still_cache.wait_for_writes()
    from atemwire.imaging import atem_to_rgb
    kept = Image.open(io.BytesIO(still_cache.get(h))).convert('RGBA').tobytes()
    assert kept == atem_to_rgb(raw, 16, 8)
    assert w._slots[1]['thumb'] is not None
    W._thumb_cache.clear()


def test_a_still_a_switcher_shows_does_not_expire():
    still_cache.put(A, b'x')
    path = storage.STILL_CACHE_DIR / f'{A}.png'
    _age(path, 6 * 86400)
    before = path.stat().st_mtime
    w = W.MediaPoolWatcher('10.9.9.8')
    w._on_file_info(_FakeMPfe(0, bytes.fromhex(A)))
    assert path.stat().st_mtime > before + 86400


# --------------------------------------------------------------------------
# the profile save
# --------------------------------------------------------------------------

class _Info:
    def __init__(self, hash_hex, name):
        self.is_used, self.hash, self.name = True, bytes.fromhex(hash_hex), name.encode()


def test_a_save_writes_kept_stills_and_downloads_only_the_rest(monkeypatch):
    from atem_control.profile import export

    files = {0: _Info(A, 'kept'), 1: _Info(B, 'new'), 2: _Info(A, 'kept')}

    class _Atem:
        connected = True
        product_name = 'ATEM Test'

        class raw:
            mixerstate = {'mediaplayer-file-info': files}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _profile():
        from atemwire import Profile
        stills = ''.join(f'<Still index="{s}" name="{i.name.decode()}" path="ATEM Media Pool/{i.name.decode()}.png"/>'
                         for s, i in sorted(files.items()))
        return Profile.from_xml(f'<Profile majorVersion="1" minorVersion="5" product="ATEM Test">'
                                f'<MediaPool><Stills>{stills}</Stills></MediaPool></Profile>')

    downloads = []

    def fake_download_media_pool_images(atem, progress_callback=None, cancel_check=None,
                                        cache_get=None, cache_put=None, **kw):
        # atemwire's loop: the cache first, a download only on a miss
        out = []
        for slot, info in sorted(files.items()):
            h = info.hash.hex()
            png = cache_get(slot, h)
            if png is None:
                downloads.append(slot)
                png = _png(colour=(slot, 0, 0, 255))
                cache_put(slot, h, png)
            out.append({'slot': slot, 'name': info.name.decode(), 'png_bytes': png})
        return out

    monkeypatch.setattr(export, 'ATEM', lambda ip: _Atem())
    monkeypatch.setattr(export.Profile, 'from_atem', staticmethod(lambda atem, opts: _profile()))
    monkeypatch.setattr(export, 'download_media_pool_images', fake_download_media_pool_images)
    still_cache.put(A, _png(colour=(9, 9, 9, 255)))

    payload, content_type, name = export.export_profile_zip(
        '10.9.9.7', export.build_save_options({}))

    assert downloads == [1]                                        # only the still nothing had seen
    assert still_cache.get(B) is not None                          # and now the cache has it
    assert content_type == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert z.read('ATEM Media Pool/kept.png') == still_cache.get(A)
        assert z.read('ATEM Media Pool/new.png') == still_cache.get(B)


# --------------------------------------------------------------------------
# the uploader
# --------------------------------------------------------------------------

def test_an_upload_seeds_the_picture_it_sent():
    from atem_control.uploader import _seed_still_cache
    from atemwire.imaging import atem_to_rgb
    raw, h = _frame()
    _seed_still_cache(raw, bytes.fromhex(h), 16, 8)
    still_cache.wait_for_writes()
    kept = Image.open(io.BytesIO(still_cache.get(h))).convert('RGBA').tobytes()
    assert kept == atem_to_rgb(raw, 16, 8)                         # what a download would give
