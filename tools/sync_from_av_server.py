#!/usr/bin/env python3
"""Sync WebATEM from the av_server monorepo (the private upstream).

WebATEM is a standalone extraction of av_server's ATEM control surface:
the vendored pyatem/pyhyperdeck libraries plus the atem_control Django app,
with the monorepo's seams (roles/auth, equipment DB, cross-app audit,
content-change uploader, multiview) replaced by thin local shims. This
script makes that extraction REPEATABLE so the fork tracks upstream
instead of rotting (the first extraction went stale in two weeks).

Three tiers:

  VERBATIM   — copied wholesale; pyatem/pyhyperdeck also get their SPDX
               header (re-)applied (upstream doesn't carry them).
  REWRITE    — copied, then mechanical seam rewrites (import paths, the
               activity/audit shim, app-local URLs).
  MANUAL     — seam files with real per-file edits (auth dropped,
               equipment lookups replaced by the switcher's own announced
               name, uploader calls pointed at the local module). The
               script never overwrites these: it compares the upstream
               file's hash against .sync_state.json and prints which ones
               changed upstream since the last sync, for hand-merging.

Excluded on purpose: everything multiview (operator decision 2026-08-23),
the Pragmatic Play program monitor (atem_pgm_monitor.js + its header dock),
and monorepo-only tests.

Usage, from the WebATEM repo root:
    python3 tools/sync_from_av_server.py            # sync + report
    python3 tools/sync_from_av_server.py --check    # report only
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

SRC = Path('/docker/repos/av_server')
DST = Path(__file__).resolve().parent.parent
STATE = DST / 'tools' / '.sync_state.json'

SPDX = '# SPDX-License-Identifier: LGPL-3.0-only\n'

# ---------------------------------------------------------------------------
# Tier lists (paths relative to each repo root)
# ---------------------------------------------------------------------------

# Copied wholesale. Directories are recursive. pyatem/pyhyperdeck sources
# additionally get the SPDX header ensured.
VERBATIM_DIRS = [
    ('pyatem', 'pyatem', True),                    # (src, dst, spdx)
    ('pyhyperdeck', 'pyhyperdeck', True),
    ('av_server/atem_control/js', 'atem_control/js', False),
    ('av_server/atem_control/static/css', 'atem_control/static/css', False),
    ('build/js', 'build/js', False),
]

# Files inside VERBATIM_DIRS that must NOT be overwritten or must not exist.
PRESERVE = [
    'pyatem/LICENSE',
    'pyatem/COPYING.GPL',
]
EXCLUDE_NAMES = {'__pycache__', '.pytest_cache', 'node_modules'}
EXCLUDE_FILES = {
    # multiview-coupled pyatem test (imports av_server.multiview upstream)
    'pyatem/tests/test_capture_retry.py',
    # WebATEM keeps its own build.sh (paths + prose differ from upstream)
    'build/js/build.sh',
}

# Copied then rewritten (single files).
REWRITE_FILES = [
    'av_server/atem_control/control/commands.py',
    'av_server/atem_control/media_pool/watcher.py',
    'av_server/atem_control/media_pool/broadcast.py',
    'av_server/atem_control/profile/dialog.py',
    'av_server/atem_control/profile/__init__.py',
    'av_server/atem_control/media_pool/__init__.py',
    'av_server/atem_control/control/__init__.py',
    'av_server/atem_control/hyperdeck/connection.py',
    'av_server/atem_control/hyperdeck/__init__.py',
    'av_server/atem_control/static/js/atem_realtime_slider.js',
    'av_server/atem_control/static/js/atem_profile.js',
    'av_server/atem_control/static/js/atem_hyperdeck.js',
    'av_server/atem_control/static/js/atem_control_page.js',
    'av_server/atem_control/static/js/atem_connect.js',
    'av_server/atem_control/static/js/atem_control.js',   # generated bundle
]

# Template sync: control page shell + every partial except the PP program
# monitor dock pieces (none today — the dock lives in _header.html, which is
# MANUAL). connect.html is MANUAL (quick-connect reads the equipment DB
# upstream).
TEMPLATE_DIR = ('av_server/atem_control/templates/control',
                'atem_control/templates/control')
TEMPLATE_SHELL = ('av_server/atem_control/templates/control.html',
                  'atem_control/templates/control.html')

# Never overwritten; hand-merged when the upstream hash changes.
MANUAL = [
    'av_server/atem_control/profile/export.py',
    'av_server/atem_control/control/consumer.py',
    'av_server/atem_control/control/views.py',
    'av_server/atem_control/control/logging.py',
    'av_server/atem_control/hyperdeck/views.py',
    'av_server/atem_control/hyperdeck/sync.py',
    'av_server/atem_control/profile/views.py',
    'av_server/atem_control/urls.py',
    'av_server/atem_control/routing.py',
    'av_server/atem_control/apps.py',
    'av_server/atem_control/models.py',
    'av_server/atem_control/templates/control/_header.html',
    'av_server/atem_control/templates/connect.html',
]

# ---------------------------------------------------------------------------
# Rewrites applied to REWRITE_FILES and synced templates/js
# ---------------------------------------------------------------------------
REWRITES = [
    # self-references: monorepo package path -> standalone app path
    (r'\bav_server\.atem_control\b', 'atem_control'),
    # audit shim: both the helpers and the ActivityLog stand-in live in
    # atem_control.activity (signature-compatible with the monorepo)
    (r'from av_server\.management\.activity import',
     'from atem_control.activity import'),
    (r'from av_server\.management\.models import ActivityLog',
     'from atem_control.activity import ActivityLog'),
    # storage shim (data-dir layout) is app-local
    (r'\bav_server\.storage\b', 'atem_control.storage'),
    # IP validation, upload executor and per-IP upload lock are app-local
    # (netutil copied verbatim; uploader absorbed at extraction;
    # ip_upload_lock is a threading stand-in for the Postgres advisory one)
    (r'\bav_server\.netutil\b', 'atem_control.netutil'),
    (r'\bav_server\.content_change\.uploader\b', 'atem_control.uploader'),
    (r'\bav_server\.content_change\.ip_upload_lock\b',
     'atem_control.ip_upload_lock'),
    # app is mounted at /atem/ and owns the drag-drop upload endpoint
    (r'/content-change/media-pool-upload/', '/atem/media-pool-upload/'),
    # path-form references in comments and build scripts
    (r'av_server/atem_control', 'atem_control'),
    # the Pragmatic Play program monitor is excluded from this build
    (r'\n\s*<script src="\{% static_v \'js/atem_pgm_monitor\.js\' %\}"></script>', ''),
    # WebATEM vendors Alpine under its own unversioned filename
    (r"vendor/alpinejs-[0-9.]+\.min\.js", 'vendor/alpine.min.js'),
    (r'Lives in av_server/ rather', 'Lives in the app rather'),
]

FORBIDDEN_AFTER = re.compile(
    r'av_server|multiview|pgm_monitor|pragmatic', re.IGNORECASE)
# Files allowed to keep a (commented) mention, checked case-sensitively
# only for imports:
IMPORT_ONLY = re.compile(r'^\s*(from|import)\s+av_server', re.MULTILINE)


def _read(p: Path) -> str:
    return p.read_text(encoding='utf-8')


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _ensure_spdx(text: str) -> str:
    return text if text.startswith(SPDX) else SPDX + text


def _apply_rewrites(text: str) -> str:
    for pat, repl in REWRITES:
        text = re.sub(pat, repl, text)
    return text


def sync(check_only: bool) -> int:
    problems = []
    copied = 0

    # --- verbatim dirs ----------------------------------------------------
    for src_rel, dst_rel, spdx in VERBATIM_DIRS:
        src_dir = SRC / src_rel
        if not src_dir.is_dir():
            problems.append(f'missing upstream dir: {src_rel}')
            continue
        for f in sorted(src_dir.rglob('*')):
            if not f.is_file():
                continue
            rel = f.relative_to(src_dir)
            if any(part in EXCLUDE_NAMES for part in rel.parts):
                continue
            dst_key = f'{dst_rel}/{rel}'.replace('\\', '/')
            if dst_key in EXCLUDE_FILES or dst_key in PRESERVE:
                continue
            dst = DST / dst_rel / rel
            text = None
            if f.suffix in {'.py', '.js', '.md', '.c', '.html', '.css',
                            '.sh', '.json', '.ini', '.typed'}:
                try:
                    text = _read(f)
                except UnicodeDecodeError:
                    text = None
            if text is not None:
                text = _apply_rewrites(text)
                if spdx and f.suffix == '.py':
                    text = _ensure_spdx(text)
                new = text.encode()
            else:
                new = f.read_bytes()
            if not check_only:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or dst.read_bytes() != new:
                    dst.write_bytes(new)
                    copied += 1

    # --- rewrite files ----------------------------------------------------
    rewrite_pairs = [(p, 'atem_control/' + p.split('atem_control/', 1)[1])
                     for p in REWRITE_FILES]
    src_tpl, dst_tpl = TEMPLATE_DIR
    for f in sorted((SRC / src_tpl).glob('*.html')):
        rel = f.name
        if f'{src_tpl}/{rel}' in MANUAL:
            continue
        rewrite_pairs.append((f'{src_tpl}/{rel}', f'{dst_tpl}/{rel}'))
    rewrite_pairs.append(TEMPLATE_SHELL)
    # the content-hash cache-bust tag lives in management/ upstream but is
    # a plain app templatetag here
    rewrite_pairs.append(('av_server/management/templatetags/static_v.py',
                          'atem_control/templatetags/static_v.py'))

    for src_rel, dst_rel in rewrite_pairs:
        f = SRC / src_rel
        if not f.is_file():
            problems.append(f'missing upstream file: {src_rel}')
            continue
        text = _apply_rewrites(_read(f))
        if IMPORT_ONLY.search(text):
            problems.append(f'unrewritten av_server import in {dst_rel}')
        dst = DST / dst_rel
        if not check_only:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or _read(dst) != text:
                dst.write_text(text, encoding='utf-8')
                copied += 1

    # --- manual tier: report upstream drift -------------------------------
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    drifted = []
    for src_rel in MANUAL:
        f = SRC / src_rel
        if not f.is_file():
            problems.append(f'missing upstream (manual): {src_rel}')
            continue
        h = _sha(f)
        if state.get(src_rel) != h:
            drifted.append(src_rel)
        state[src_rel] = h
    if not check_only:
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))

    # --- report -----------------------------------------------------------
    print(f'{"CHECK" if check_only else "SYNC"}: {copied} files written')
    if drifted:
        print('\nMANUAL-tier files changed upstream — hand-merge these:')
        for p in drifted:
            print(f'  {p}')
    if problems:
        print('\nPROBLEMS:')
        for p in problems:
            print(f'  {p}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(sync('--check' in sys.argv))
