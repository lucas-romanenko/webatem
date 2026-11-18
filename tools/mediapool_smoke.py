#!/usr/bin/env python3
"""
Smoke test for ``atem_control.media_pool`` against a live ATEM.

Exercises the public API: acquire a watcher, subscribe to its events,
wait for the supervisor to connect and emit a snapshot, then read the
snapshot and release.

Exits 0 if at least one used slot ends up with a base64-encoded thumbnail,
non-zero otherwise.

Usage:
    python tools/mediapool_smoke.py 192.0.2.10
"""

import logging
import os
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(name)-18s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
)
logging.getLogger('AtemProtocol').setLevel(logging.INFO)
logging.getLogger('UdpTransport').setLevel(logging.WARNING)

# Run from a checkout (python tools/<script>.py): the repo root on the path,
# and Django configured, since the hooks read settings. A host's own
# settings win when DJANGO_SETTINGS_MODULE is set (inside AV Server's
# container, say).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webatem.settings')
import django  # noqa: E402
django.setup()

from atem_control.media_pool import watcher as mps  # noqa: E402


# Budget to wait for at least one thumbnail to land. Raw-socket download
# of a 1080p still takes ~8s; back-fill for multiple slots stacks up.
THUMBNAIL_WAIT_SECONDS = 60.0


def main():
    if len(sys.argv) < 2:
        print("Usage: mediapool_smoke.py <ip>", file=sys.stderr)
        sys.exit(2)
    ip = sys.argv[1]

    any_thumb = threading.Event()

    def _on_snapshot(data):
        slots = data.get('slots') or []
        players = data.get('players') or []
        print(f"  [snapshot] {len(slots)} slots, {len(players)} players")
        if any(s.get('thumb') for s in slots):
            any_thumb.set()

    def _on_slot_updated(data):
        s = data.get('slot') or {}
        thumb_note = 'thumb' if s.get('thumb') else 'no-thumb'
        print(f"  [slot_updated] slot {s.get('index')} "
              f"hash={s.get('hash','')[:8]}... ({thumb_note})")
        if s.get('thumb'):
            any_thumb.set()

    def _on_player_updated(data):
        p = data.get('player') or {}
        print(f"  [player_updated] mp{p.get('index')} type={p.get('type')}")

    # Module-level acquire creates the watcher if needed, starts threads,
    # bumps ref_count, and returns the current snapshot.
    print(f"Acquiring watcher for {ip}...")
    initial = mps.acquire(ip)
    print(f"  initial snapshot: {len(initial.get('slots', []))} slots, "
          f"{len(initial.get('players', []))} players")

    # Subscribe after acquire so our handlers see the back-fill events when
    # the supervisor reaches _on_connected.
    watcher = mps._watchers[ip]
    watcher.on('snapshot', _on_snapshot)
    watcher.on('slot_updated', _on_slot_updated)
    watcher.on('player_updated', _on_player_updated)

    print(f"Waiting up to {THUMBNAIL_WAIT_SECONDS:.0f}s for a thumbnail...")
    any_thumb.wait(timeout=THUMBNAIL_WAIT_SECONDS)

    snap = mps.get_snapshot(ip)
    used = [s for s in snap['slots'] if s['isUsed']]
    with_thumb = [s for s in used if s['thumb']]
    print(f"Final snapshot: {len(snap['slots'])} slots total, "
          f"{len(used)} used, {len(with_thumb)} with thumbnails")
    for s in used[:5]:
        print(f"  slot {s['index']:>2}: hash={s['hash'][:8]}... "
              f"name={s['fileName']!r} thumb={'yes' if s['thumb'] else 'no'}")
    if len(used) > 5:
        print(f"  ... ({len(used) - 5} more)")
    print(f"Players: {len(snap['players'])}")
    for p in snap['players']:
        print(f"  mp{p['index']}: type={p['type']} "
              f"still={p['stillIndex']} clip={p['clipIndex']}")

    mps.release(ip)
    # Let the 5s watcher grace period elapse so the supervisor stops and
    # the daemon threads exit cleanly before the process does.
    time.sleep(6)

    if not with_thumb:
        if used:
            print("FAIL: used slots found but no thumbnails landed in budget",
                  file=sys.stderr)
        else:
            print("FAIL: no used slots on this ATEM — nothing to test",
                  file=sys.stderr)
        sys.exit(1)

    print("OK")
    sys.exit(0)


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
