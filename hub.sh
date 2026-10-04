#!/usr/bin/env bash
# Launch the Command Center from a desktop icon. This is what the dock runs.
#
# THE MODEL: the server is the instrument, the window is a view of it.
#
# `hub.py` owns the BLE link and holds a recording in memory until the run ends
# (experiments.Recorder.save writes at stop, not as it goes). So closing the
# window must not stop the server: an accidental click on the X would drop the
# link and lose an in-flight capture with nothing on disk to show for it.
#
#   click the icon   -> server if none is up, then a window onto it
#   click it again   -> another window onto the SAME server, never a second one
#   close the window -> nothing happens to the server or the device
#   "Release device" -> stops the server, and refuses while a run is going
#
# A dock click has NO terminal attached, so anything printed on the way to
# dying goes nowhere and the icon just bounces. Everything here is teed to a
# log, and a failure puts the tail of it on screen.
#
# IT ALSO CHOOSES THE INTERPRETER, and does not trust PATH to do it. A desktop
# session's PATH is not the shell's: the owner's terminal resolves `python3` to a
# conda environment that has bleak, the dock resolves it to /usr/bin/python3,
# which does not. Trusting PATH means the icon and the terminal run different
# programs. Same lesson as joystick/joystick.sh, same fix.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8080
URL="http://127.0.0.1:$PORT"
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/command-center.log"
PIDFILE="${XDG_RUNTIME_DIR:-/tmp}/command-center.pid"
mkdir -p "$(dirname "$LOG")"

say() { echo "$*" >>"$LOG"; }
say "=== $(date -Is)  hub.sh $* from $HERE"

# Errors have to reach a screen, because there is no terminal behind the icon.
fail() {
  say "FAILED: $1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --no-wrap --title="Command Center" \
      --text="$(printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')

$(printf '%s' "$(tail -n 15 "$LOG")" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')" 2>/dev/null
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Command Center" "$1 — see $LOG"
  fi
  exit 1
}

# The page serves with or without a device, so a live server answering /api/status
# is the whole readiness test. Two seconds is generous for a loopback request.
status_json() { curl -fsS --max-time 2 "$URL/api/status" 2>/dev/null; }
hub_up() { status_json >/dev/null; }

# ---------------------------------------------------------------------------
# Release device: stop the server, but never mid-run.
#
# `running` is the name of the experiment in flight, or null. Refusing here is
# the point of the action: the samples for that run are in the server's memory
# and nowhere else, so killing it discards them silently.
if [ "${1:-}" = "--quit" ]; then
  js="$(status_json)" || { say "nothing serving on $PORT"; exit 0; }
  run="$(printf '%s' "$js" | python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("running") or "")' 2>/dev/null)"
  if [ -n "$run" ]; then
    say "refused: '$run' is running"
    zenity --warning --no-wrap --title="Command Center" \
      --text="<b>$run</b> is recording.\n\nThe samples for this run are in memory and are only written to\ndisk when the run ends. Stop it from the page first." 2>/dev/null \
      || notify-send -u critical "Command Center" "$run is recording — stop it from the page first" 2>/dev/null
    exit 1
  fi
  pid=""
  [ -r "$PIDFILE" ] && pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || pid="$(pgrep -f "[h]ub\.py" | head -1)"
  if [ -n "$pid" ]; then
    say "stopping hub pid $pid"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    command -v notify-send >/dev/null 2>&1 &&
      notify-send "Command Center" "Server stopped, device released" 2>/dev/null
  else
    say "serving, but no pid found — leaving it alone"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Start the server, unless one is already serving.
#
# hub.py binds 127.0.0.1:8080 unconditionally, so a second instance dies on
# "address in use". That is exactly what a second dock click would do, which is
# why this checks first and opens another window onto the running one instead.
if hub_up; then
  say "hub already serving on $PORT"
else
  # What the server cannot start without, per host/README.md. Probed with
  # importlib.util.find_spec, which LOCATES a module without executing it:
  # importing numpy for real, once per candidate, would add seconds to a launch.
  # pyserial and scipy are optional and deliberately not required here.
  # `intomind` is the instrument API, which lives in its own repository since
  # the 2026-08-19 split and is installed rather than found on a relative path.
  # An interpreter that has aiohttp but not intomind now fails this probe with
  # a dialog naming the missing package, instead of dying on an ImportError
  # with no terminal attached.
  REQUIRED="aiohttp bleak numpy intomind"

  probe() {   # $1 = interpreter; prints the modules it is missing
    "$1" -c '
import importlib.util, sys
print(" ".join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))
' $REQUIRED 2>/dev/null
  }

  # In order, so an explicit answer always beats a guess. Environments are
  # globbed rather than named: naming one breaks the day it is rebuilt.
  CANDIDATES=()
  [ -n "${COMMAND_CENTER_PYTHON:-}" ] && CANDIDATES+=("$COMMAND_CENTER_PYTHON")
  [ -n "${CONDA_PREFIX:-}" ] && CANDIDATES+=("$CONDA_PREFIX/bin/python3")
  CANDIDATES+=("$(command -v python3 2>/dev/null || true)")
  for d in "$HOME"/miniconda3/envs/*/bin/python3 "$HOME"/anaconda3/envs/*/bin/python3 \
           "$HOME"/.venv/bin/python3 "$HERE"/.venv/bin/python3 /usr/bin/python3; do
    [ -x "$d" ] && CANDIDATES+=("$d")
  done

  PY=""; REPORT=""
  for c in "${CANDIDATES[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    miss="$(probe "$c")"
    if [ -z "$miss" ]; then PY="$c"; break; fi
    REPORT="$REPORT
  $c — missing:$miss"
  done
  [ -n "$PY" ] || fail "No Python has everything the Command Center needs ($REQUIRED).
Tried:$REPORT

Install the missing package, or set COMMAND_CENTER_PYTHON to an interpreter that has them."

  say "    interpreter: $PY"
  # setsid so the server outlives this script, this terminal and the window.
  # The link to a device someone may be wearing is not something a closing
  # window gets to drop.
  setsid nohup "$PY" "$HERE/hub.py" >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  say "    started pid $(cat "$PIDFILE")"

  # It binds the port before it goes looking for a device, so this waits on the
  # port and not on the BLE scan. 15 s covers an import of numpy on a cold page
  # cache; past that something is actually wrong.
  ready=""
  for _ in $(seq 1 75); do
    if hub_up; then ready=1; break; fi
    kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break
    sleep 0.2
  done
  [ -n "$ready" ] || fail "The server did not come up on $URL within 15 s."
fi

# ---------------------------------------------------------------------------
# The window.
#
# Chrome's --app gives a window with no tab strip and no address bar, which is
# what makes it read as an application rather than a browser. --class sets the
# window's identity so the shell groups it under this icon instead of under
# Chrome. Falling back to the default browser costs a tab strip, not the app.
open_window() {
  for c in google-chrome chromium chromium-browser brave-browser microsoft-edge; do
    if command -v "$c" >/dev/null 2>&1; then
      say "    window: $c --app"
      setsid nohup "$c" --app="$URL" --class=command-center --name=command-center \
        >>"$LOG" 2>&1 &
      return 0
    fi
  done
  say "    window: xdg-open (no Chromium-family browser found)"
  setsid nohup xdg-open "$URL" >>"$LOG" 2>&1 &
}
open_window
exit 0
