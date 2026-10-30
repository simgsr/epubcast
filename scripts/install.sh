#!/usr/bin/env bash
# epubcast bootstrap: one idempotent command that builds a complete, pinned environment.
#
#   ./scripts/install.sh        # everything the app needs: CLI, GUI (macOS), Edge voices
#   ./scripts/install.sh --mlx  # also the Apple-GPU accelerator (Apple Silicon only)
#
# Safe to re-run: an existing .venv is reused, already-satisfied pins are skipped, and the
# project is reinstalled editable so a `git pull` is picked up by simply re-running this.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENV="$ROOT/.venv"
LOCK="$ROOT/requirements.lock"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '\033[33mwarn\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

WITH_MLX=0
ALLOW_BREW=1
for arg in "$@"; do
  case "$arg" in
    --mlx)     WITH_MLX=1 ;;
    --no-brew) ALLOW_BREW=0 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

IS_MAC=0
if [ "$(uname -s)" = Darwin ]; then IS_MAC=1; fi

# --- 1. Interpreter ---------------------------------------------------------------------
# pyproject.toml requires >=3.10,<3.13, and spacy publishes no wheels for 3.13+. Homebrew's
# plain `python3` is already newer than that on a current machine, so never ask for `python3`.
IN_RANGE='import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)'
find_python() {
  for candidate in "$VENV/bin/python" python3.12 python3.11 python3.10 /opt/homebrew/bin/python3.12; do
    if path=$(command -v "$candidate" 2>/dev/null) && "$path" -c "$IN_RANGE" 2>/dev/null; then
      printf '%s' "$path"
      return 0
    fi
  done
  return 1
}

PY=$(find_python || true)
if [ -z "$PY" ] && [ "$ALLOW_BREW" = 1 ] && command -v brew >/dev/null 2>&1; then
  # Homebrew's own `python3` is 3.13+ now, which this project does not support: install the
  # versioned keg rather than telling the user to go and work out the range themselves.
  say "Installing Python 3.12 with Homebrew (this project supports 3.10-3.12)"
  brew install python@3.12
  PY=$(find_python || true)
fi
[ -n "$PY" ] || die "Python 3.10-3.12 is required; install it with: brew install python@3.12"

# A .venv built by a different interpreter cannot be reused: recreate it.
if [ -d "$VENV" ] && ! "$VENV/bin/python" -c "$IN_RANGE" 2>/dev/null; then
  warn "recreating .venv: it was built with an unsupported Python"
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  say "Creating .venv with $("$PY" -V 2>&1)"
  "$PY" -m venv "$VENV"
fi
PIP=("$VENV/bin/python" -m pip)

# --- 2. System tools --------------------------------------------------------------------
# ffmpeg writes the .m4b and espeak-ng feeds phonemization: without them the app installs
# cleanly and then fails at the last step of a conversion, so they are a hard requirement.
MISSING=()
command -v ffmpeg    >/dev/null 2>&1 || MISSING+=(ffmpeg)
command -v espeak-ng >/dev/null 2>&1 || MISSING+=(espeak-ng)
if [ ${#MISSING[@]} -gt 0 ]; then
  if [ "$ALLOW_BREW" = 1 ] && command -v brew >/dev/null 2>&1; then
    say "Installing missing system tools with Homebrew: ${MISSING[*]}"
    brew install "${MISSING[@]}"
  else
    die "missing system tools: ${MISSING[*]} — install them with: brew install ${MISSING[*]}"
  fi
  for tool in "${MISSING[@]}"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is still not on PATH after installing it"
  done
fi

# --- 3. Dependencies --------------------------------------------------------------------
# requirements.lock is exported from poetry.lock: core plus the [edge] extra and, on macOS,
# the [gui] extra. Shipping one lock is what makes a single command enough for both the
# command line and the desktop UI.
say "Installing pinned dependencies from requirements.lock"
"${PIP[@]}" install --quiet --upgrade pip
"${PIP[@]}" install --quiet -r "$LOCK"

# --- 4. The project itself --------------------------------------------------------------
# Editable + --no-deps: the dependencies are already pinned above, and an editable install
# means the `epubcast` command tracks the working tree instead of a frozen copy.
say "Installing epubcast (editable)"
"${PIP[@]}" install --quiet --no-deps -e "$ROOT"

if [ "$WITH_MLX" = 1 ]; then
  say "Installing the [mlx] extra"
  "${PIP[@]}" install --quiet -e "$ROOT[mlx]"
fi

# --- 5. Verify --------------------------------------------------------------------------
# Installing cleanly is not the same as running: check what a first conversion and a first
# GUI launch actually touch, so a broken environment fails here and not mid-run.
say "Verifying"
"$VENV/bin/epubcast" --help >/dev/null 2>&1 \
  || die "the epubcast command did not run; re-run with: bash -x scripts/install.sh"
"$VENV/bin/python" - <<'PY' || die "the CLI installed but its dependencies do not import"
from epubcast.backends import resolve_backend, torch_available
raise SystemExit(0 if torch_available() else 'the torch backend (kokoro) did not install')
PY
ok "command line (backend: $("$VENV/bin/python" -c 'from epubcast.backends import resolve_backend; print(resolve_backend("auto"))'))"
if [ "$IS_MAC" = 1 ]; then
  "$VENV/bin/python" -c 'import wx, PIL, epubcast.ui' 2>/dev/null \
    || die "the desktop UI did not install; run: $VENV/bin/python -m pip install -e '.[gui]'"
  ok "desktop UI (wxPython $("$VENV/bin/python" -c 'import wx; print(wx.__version__)'))"
else
  warn "desktop UI skipped: it is macOS-only in this fork"
fi
ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
ok "espeak-ng on PATH"

say "Done."
cat <<EOF

Run it:

  $VENV/bin/epubcast book.epub -v af_sky
EOF
if [ "$IS_MAC" = 1 ]; then
  printf '  %s\n' "$VENV/bin/epubcast-ui      (desktop GUI)"
fi
cat <<EOF

or activate the environment once per shell and drop the prefix:

  source $VENV/bin/activate

Output lands in ./audiobooks/ (created if missing). See README for voices and backends.
EOF
