#!/usr/bin/env bash
# Put the Command Center in the desktop's application list, so it can be
# launched from an icon and pinned to the dock.
#
#   ./install-launcher.sh              install for the current user
#   ./install-launcher.sh --uninstall  take it back out
#
# The .desktop file is GENERATED here rather than committed, because it has to
# name an absolute path to hub.sh and that path is different on every machine.
# Nothing installed by this script is inside the repo, and nothing in the repo
# depends on having run it: `python3 hub.py` is unaffected either way.
#
# This is Linux desktop integration (freedesktop .desktop entries). It is the
# only part of the Command Center that is not portable, which is why it lives
# in a separate script that the product does not need.
set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
ID="command-center"

refresh() {
  command -v update-desktop-database >/dev/null 2>&1 &&
    update-desktop-database "$APPS" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 &&
    gtk-update-icon-cache -f -t "${ICONS%/scalable/apps}" 2>/dev/null || true
}

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$APPS/$ID.desktop" "$ICONS/$ID.svg"
  refresh
  echo "removed $APPS/$ID.desktop"
  echo "the server, if one is running, was left alone: $HERE/hub.sh --quit"
  exit 0
fi

mkdir -p "$APPS" "$ICONS"
chmod +x "$HERE/hub.sh"
cp "$HERE/assets/command-center.svg" "$ICONS/$ID.svg"

# StartupWMClass has to match what the window actually calls itself, or the
# shell shows a second, generic icon while the app is open instead of lighting
# up this one. hub.sh passes --class=command-center to the browser to make it so.
#
# UNVERIFIED ON WAYLAND. Under X11 that flag sets WM_CLASS and this matches. A
# GNOME Wayland session matches on the surface's app_id instead, and it was not
# possible to read one back here: GNOME 46 refuses both Shell.Introspect and
# Shell.Screenshot to an unprivileged caller, and a native Wayland window is
# invisible to xprop. If a second icon appears in the dock while the window is
# open, the value to try instead is `chrome-127.0.0.1__8080-Default`, which is
# the identity Chrome derives from the URL for an --app window.
cat >"$APPS/$ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Command Center
GenericName=Biopotential instrument
Comment=Run the instrument, record sessions, analyze them
Exec=$HERE/hub.sh
Path=$HERE
Icon=$ID
Terminal=false
Categories=Science;MedicalSoftware;
Keywords=EEG;biopotential;instrument;recording;IntoMind;
StartupNotify=true
StartupWMClass=command-center
# Closing the window deliberately leaves the server up: it owns the link to the
# device and holds an in-flight recording in memory. This is how you actually
# stop it. It refuses while a run is going.
Actions=release;

[Desktop Action release]
Name=Release device (stop server)
Exec=$HERE/hub.sh --quit
EOF
chmod +x "$APPS/$ID.desktop"
refresh

echo "installed $APPS/$ID.desktop"
echo "  runs: $HERE/hub.sh"
echo "  icon: $ICONS/$ID.svg"
echo
echo "Search for 'Command Center' in the application list, then right-click its"
echo "icon in the dock and pin it. Right-click also has 'Release device'."
