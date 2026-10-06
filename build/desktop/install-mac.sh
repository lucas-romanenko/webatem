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
# Uninstall, the same way:
#
#   curl -fsSL https://github.com/lucas-romanenko/webatem/releases/latest/download/install-mac.sh | sh -s -- --uninstall
#
# For when the app is already in the Trash (its own Uninstall button is the
# usual way): removes WebATEM.app, the settings and connection history, the
# login item and the launcher window's WebKit data. macOS has no uninstall
# step, so a Trash drag leaves those behind, and the login item would try to
# start a program that is gone at every login.
#
# For the build workflow's own test: WEBATEM_DMG=<path> installs a local
# disk image instead of downloading, WEBATEM_DEST=<dir> installs (and
# uninstalls) somewhere other than /Applications, WEBATEM_NO_OPEN=1 does not
# launch it.
set -eu

repo="lucas-romanenko/webatem"
bundle_id="com.webatem.app"
case "$(uname -s)" in Darwin) ;; *) echo "This installer is for macOS. Downloads for other systems: https://github.com/$repo/releases/latest" >&2; exit 1 ;; esac

if [ "${1:-}" = "--uninstall" ]; then
  # A running copy holds its files open; end it the way the installer does.
  pkill -x webatem 2>/dev/null && sleep 1 || true
  removed=0
  for app in ${WEBATEM_DEST:+"$WEBATEM_DEST/WebATEM.app"} /Applications/WebATEM.app "$HOME/Applications/WebATEM.app"; do
    [ -d "$app" ] || continue
    # Only our own bundle, never a folder that happens to have the name.
    [ "$(defaults read "$app/Contents/Info" CFBundleIdentifier 2>/dev/null || true)" = "$bundle_id" ] || continue
    rm -rf "$app" && echo "Removed $app" && removed=1
  done
  for left in \
    "$HOME/Library/Application Support/WebATEM" \
    "$HOME/Library/LaunchAgents/$bundle_id.plist" \
    "$HOME/Library/WebKit/$bundle_id" \
    "$HOME/Library/Caches/$bundle_id" \
    "$HOME/Library/HTTPStorages/$bundle_id" \
    "$HOME/Library/HTTPStorages/$bundle_id.binarycookies" \
    "$HOME/Library/Saved Application State/$bundle_id.savedState" \
    "$HOME/Library/Preferences/$bundle_id.plist"; do
    if [ -e "$left" ]; then rm -rf "$left" && echo "Removed $left" && removed=1; fi
  done
  [ "$removed" = 1 ] && echo "WebATEM is uninstalled." || echo "Nothing of WebATEM was left on this Mac."
  exit 0
fi
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
