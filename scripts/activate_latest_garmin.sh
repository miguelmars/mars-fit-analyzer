#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STAGING_DIR="$ROOT/data/garmin_reference_2026_06_08"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=python3
fi

if [ ! -f "$STAGING_DIR/garmin_activities_clean.json.gz" ]; then
  echo "Missing bundled Garmin snapshot: $STAGING_DIR" >&2
  exit 1
fi

cd "$ROOT"

"$PYTHON_BIN" -c \
  "from tools.auto_activate_garmin import activate_bundled_snapshot; print(activate_bundled_snapshot())"
