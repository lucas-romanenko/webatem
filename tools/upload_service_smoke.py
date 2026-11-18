#!/usr/bin/env python3
"""
Smoke test for atem_control.uploader.execute_upload against a
live ATEM. Uploads one still to the given slot and reports the per-item
result. WRITES the switcher's media pool: a test ATEM only.

Usage:
    python tools/upload_service_smoke.py 192.0.2.10 still.png [slot]
"""
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

# Run from a checkout (python tools/<script>.py): the repo root on the path,
# and Django configured, since the hooks read settings. A host's own
# settings win when DJANGO_SETTINGS_MODULE is set.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webatem.settings')
import django  # noqa: E402
django.setup()

from atem_control.uploader import execute_upload  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print("Usage: upload_service_smoke.py <ip> <image_path> [slot]", file=sys.stderr)
        sys.exit(2)
    ip = sys.argv[1]
    image = sys.argv[2]
    slot = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    if not os.path.isfile(image):
        print(f"FAIL: {image} not found", file=sys.stderr)
        sys.exit(1)

    t_start = time.time()
    results = execute_upload(
        [(ip, slot, image)],
        skip_tally=True,
    )
    total_seconds = time.time() - t_start

    item = results[0] if results else None
    print("\n=== RESULT ===")
    print(f"success:       {item.success if item else False}")
    if item:
        print(f"slot:          {item.slot}")
        print(f"upload_secs:   {item.upload_seconds:.2f}")
        print(f"settle_secs:   {item.settle_seconds:.2f}")
        if item.error:
            print(f"error:         {item.error}")
    print(f"total_seconds: {total_seconds:.2f}")

    sys.exit(0 if item and item.success else 1)


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
