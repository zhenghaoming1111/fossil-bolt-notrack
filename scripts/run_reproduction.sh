#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Missing python3. Install Python 3 on the Linux host, then rerun this command.\n' >&2
  exit 2
fi
exec python3 "$root/scripts/reproduction_host.py" "$@"
