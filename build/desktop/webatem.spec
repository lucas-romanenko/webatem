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
for pkg in ('atem_control', 'config'):
    hiddenimports += collect_submodules(pkg)
    datas += collect_data_files(pkg)

# Third-party packages that lean on dynamic imports / ship data. atemwire and
# hyperdeckwire are the Blackmagic device libraries (PyPI); collect_all takes
# atemwire's compiled mediaconvert extension along with the modules.
for pkg in ('django', 'channels', 'whitenoise', 'uvicorn', 'zeroconf', 'PIL', 'atemwire', 'hyperdeckwire'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Top-level template dir (config.TEMPLATES DIRS = BASE_DIR/'templates') and
# the collected static root — neither belongs to a package.
datas += [(rel('templates'), 'templates')]
datas += [(rel('staticfiles'), 'staticfiles')]

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
    [rel('launcher.py')],
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='webatem',
    debug=False,
    strip=False,
    upx=False,
    # macOS: windowed (no terminal) so the .app double-clicks like a real
    # app. Windows/Linux: console so `WebATEM is running…` + Ctrl-C work.
    console=not IS_MAC,
    icon=ICON,
    disable_windowed_traceback=False,
)

# macOS: wrap the executable in a proper WebATEM.app bundle (icon in Finder
# and the Dock; quit via the Dock icon). CI ad-hoc-signs it and packages a
# drag-to-Applications .dmg.
if IS_MAC:
    app = BUNDLE(
        exe,
        name='WebATEM.app',
        icon=rel('build', 'desktop', 'icon.icns'),
        bundle_identifier='com.webatem.app',
        info_plist={
            'CFBundleName': 'WebATEM',
            'CFBundleDisplayName': 'WebATEM',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.video',
        },
    )
