# PyInstaller spec — WebATEM native desktop launcher (one self-contained
# executable per OS). Run from the repo root:
#   pyinstaller build/desktop/webatem.spec
# collectstatic MUST run before this (staticfiles/ is bundled read-only and
# served by WhiteNoise). The compiled webatem.css must already sit in
# atem_control/static/vendor/ so collectstatic picks it up.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

# SPECPATH is build/desktop/ — the repo root is two levels up. Anchor every
# path to it so the build works regardless of the cwd, and put the repo on
# sys.path so collect_* can import the first-party packages during analysis.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

IS_MAC = sys.platform == 'darwin'
IS_WIN = sys.platform == 'win32'


def rel(*parts):
    return os.path.join(ROOT, *parts)


# Per-OS app icon. On Windows it's embedded in the .exe; on macOS it goes on
# the .app bundle (below). A Linux ELF carries no icon.
def _version():
    """The version in pyproject.toml — stamped into the Windows exe."""
    import re
    text = open(rel('pyproject.toml'), encoding='utf-8').read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else '0.0.0'


def _win_version_file():
    """A VERSIONINFO resource: product, company, version. A frozen exe with
    no metadata at all scores worse with Windows' heuristic scanners than
    one that says what it is, and every real application carries it."""
    import tempfile
    parts = ([int(p) for p in _version().split('.') if p.isdigit()] + [0, 0, 0, 0])[:4]
    body = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(parts)}, prodvers={tuple(parts)}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'Lucas Romanenko'),
        StringStruct('FileDescription', 'WebATEM - browser control for ATEM switchers'),
        StringStruct('FileVersion', '{_version()}'),
        StringStruct('InternalName', 'webatem'),
        # WebATEM's own code is MIT, but this bundle is a Combined Work: it
        # contains atemwire and pystray, both LGPL-3.0. Saying only "MIT"
        # here would misstate what is inside. Their licence texts ride along
        # in the bundle (dist-info/licenses), and the full source is public,
        # so a user can rebuild against their own atemwire.
        StringStruct('LegalCopyright', 'MIT, and bundles atemwire and pystray under LGPL-3.0. Not affiliated with the switcher manufacturer.'),
        StringStruct('OriginalFilename', 'webatem.exe'),
        StringStruct('ProductName', 'WebATEM'),
        StringStruct('ProductVersion', '{_version()}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = os.path.join(tempfile.mkdtemp(prefix='webatem-ver-'), 'version_info.txt')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(body)
    return path


if IS_WIN:
    ICON = rel('build', 'desktop', 'icon.ico')
elif IS_MAC:
    ICON = rel('build', 'desktop', 'icon.icns')
else:
    ICON = None


datas = []
binaries = []
hiddenimports = []

# First-party packages: pull every submodule (migrations/apps/admin are
# imported dynamically by Django and PyInstaller can't see them statically)
# plus their template/static data files.
for pkg in ('atem_control', 'webatem'):
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)

# Third-party packages that lean on dynamic imports / ship data. atemwire and
# hyperdeckwire are the Blackmagic device libraries (PyPI); collect_all takes
# atemwire's compiled mediaconvert extension along with the modules.
for pkg in ('django', 'channels', 'whitenoise', 'uvicorn', 'zeroconf', 'PIL', 'atemwire', 'hyperdeckwire', 'pystray', 'webview', 'bottle', 'proxy_tools'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# (webatem/templates and atem_control/static ride in with the package data
# above; the launcher collects static into the data dir at start.)

# The tray's per-OS backend is chosen at runtime (pystray._darwin / _win32 /
# _xorg / _appindicator); on macOS it sits on PyObjC, whose frameworks load
# dynamically too.
if IS_MAC:
    for pkg in ('objc', 'AppKit', 'Foundation', 'Quartz', 'WebKit', 'Security', 'UniformTypeIdentifiers'):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
        except Exception:
            pass

# The launcher window on Windows is WebView2 through pythonnet.
if IS_WIN:
    hiddenimports += ['clr', 'clr_loader', 'webview.platforms.edgechromium', 'webview.platforms.winforms']

# uvicorn/asgi bits PyInstaller routinely misses.
hiddenimports += [
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.lifespan.on',
    'uvicorn.loops.asyncio',
    'websockets.legacy',
    'django.contrib.staticfiles',
    'whitenoise.storage',
]

a = Analysis(
    [rel('webatem', 'launcher.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'PyInstaller', 'django.contrib.postgres'],
    noarchive=False,
)

pyz = PYZ(a.pure)

# macOS and Windows are onedir; Linux stays onefile (one downloadable file,
# and no scanner in the way). A onefile executable unpacks its 30-odd MB into
# a temp directory at EVERY start — slow, because the launcher window is this
# same program started again (0.5.1 moved macOS off it) — and, on Windows,
# self-extracting-unsigned is exactly the shape heuristic virus scanners
# score worst: 0.6.3's download was blocked outright. A plain folder of DLLs
# beside an exe with real version metadata is ordinary software.
DIR_BUILD = IS_MAC or IS_WIN
exe = EXE(
    pyz,
    a.scripts,
    *([] if DIR_BUILD else [a.binaries, a.datas]),
    [],
    exclude_binaries=DIR_BUILD,
    name='webatem',
    debug=False,
    strip=False,
    upx=False,
    # macOS and Windows: windowed — the app lives in the menu bar / system
    # tray and quits from there (output goes to webatem.log in the data
    # dir). Linux: console, so a terminal start shows the address and
    # Ctrl-C works on a headless box.
    console=not (IS_MAC or IS_WIN),
    icon=ICON,
    version=_win_version_file() if IS_WIN else None,
    disable_windowed_traceback=False,
)

if IS_WIN:
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WebATEM')

# macOS: wrap the executable in a proper WebATEM.app bundle (icon in Finder
# and the Dock; quit via the Dock icon). CI ad-hoc-signs it and packages a
# drag-to-Applications .dmg.
if IS_MAC:
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='webatem')
    app = BUNDLE(
        coll,
        name='WebATEM.app',
        icon=rel('build', 'desktop', 'icon.icns'),
        bundle_identifier='com.webatem.app',
        info_plist={
            'CFBundleName': 'WebATEM',
            'CFBundleDisplayName': 'WebATEM',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            # Menu-bar app: no Dock icon, no app menu; Quit lives in the
            # status item's menu (like Companion).
            'LSUIElement': True,
            'LSApplicationCategoryType': 'public.app-category.video',
        },
    )
