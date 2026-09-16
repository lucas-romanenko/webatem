#!/bin/sh
# WebATEM for macOS, from Terminal:
#
#   curl -fsSL https://github.com/lucas-romanenko/webatem/releases/latest/download/install-mac.sh | sh
#
# Downloads the disk image for this Mac's chip, copies WebATEM.app into
# /Applications (or ~/Applications when that is not writable) and opens it.
# Why this exists: a browser marks what it downloads as quarantined, and
# Gatekeeper then refuses an app that is not notarized by Apple until the
# user goes to System Settings -> Privacy & Security -> Open Anyway. curl
# sets no such mark, so an app installed this way opens like any other.
# The builds are ad-hoc signed and not notarized, which is a paid Apple
# Developer membership WebATEM does not have.
#
# For the build workflow's own test: WEBATEM_DMG=<path> installs a local
# disk image instead of downloading, WEBATEM_DEST=<dir> installs somewhere
# other than /Applications, WEBATEM_NO_OPEN=1 does not launch it.
set -eu

repo="lucas-romanenko/webatem"
case "$(uname -s)" in Darwin) ;; *) echo "This installer is for macOS. Downloads for other systems: https://github.com/$repo/releases/latest" >&2; exit 1 ;; esac
case "$(uname -m)" in
  arm64)  asset="webatem-macos-arm64.dmg" ;;
  x86_64) asset="webatem-macos-intel.dmg" ;;
  *)      echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
esac

tmp="$(mktemp -d "${TMPDIR:-/tmp}/webatem.XXXXXX")"
mnt="$tmp/dmg"
cleanup() { hdiutil detach "$mnt" -quiet 2>/dev/null || true; rm -rf "$tmp"; }
trap cleanup EXIT

dmg="${WEBATEM_DMG:-}"
if [ -z "$dmg" ]; then
  dmg="$tmp/$asset"
  echo "Downloading $asset ..."
  curl -fSL --progress-bar "https://github.com/$repo/releases/latest/download/$asset" -o "$dmg"
fi

mkdir -p "$mnt"
hdiutil attach "$dmg" -nobrowse -readonly -quiet -mountpoint "$mnt"
[ -d "$mnt/WebATEM.app" ] || { echo "No WebATEM.app inside $asset" >&2; exit 1; }

dest="${WEBATEM_DEST:-/Applications}"
if [ ! -w "$dest" ]; then
  dest="$HOME/Applications"; mkdir -p "$dest"
fi
# A running copy holds files open; end it the way its own Quit does.
pkill -x webatem 2>/dev/null && sleep 1 || true
rm -rf "$dest/WebATEM.app"
cp -R "$mnt/WebATEM.app" "$dest/WebATEM.app"
# Belt and braces: nothing here should carry the mark, but a disk image a
# browser downloaded earlier and passed in via WEBATEM_DMG would.
xattr -dr com.apple.quarantine "$dest/WebATEM.app" 2>/dev/null || true
hdiutil detach "$mnt" -quiet

echo "WebATEM is installed in $dest."
if [ -z "${WEBATEM_NO_OPEN:-}" ]; then
  open "$dest/WebATEM.app"
  echo "It is starting: look for the WebATEM icon in the menu bar."
else
  echo "Not opened (WEBATEM_NO_OPEN is set)."
fi
