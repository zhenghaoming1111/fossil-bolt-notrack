#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

mode=${1:-}
case "$mode" in
  verify)
    sha256sum --check SOURCE_SHA256SUMS --quiet
    if [[ -f CONTAINER_SHA256SUMS ]]; then
      sha256sum --check CONTAINER_SHA256SUMS --quiet
    fi
    exec python3 scripts/verify_fossil_submission.py
    ;;
  check|benchmark|negative-check|negative) ;;
  *) printf 'Usage: %s {verify|check|benchmark|negative-check|negative} [new-output-dir]\n' "$0" >&2; exit 2 ;;
esac

out=${2:?Provide a new output directory}
out=$(realpath -m -- "$out")
if [[ -e "$out" ]]; then
  printf 'Output already exists: %s\n' "$out" >&2
  exit 2
fi
tools=${TOOLS_ROOT:-"$root/artifacts/mainline-options"}
original=${FOSSIL_ORIGINAL:-"$root/artifacts/source-fossil/bin/fossil.orig"}
if [[ -f CONTAINER_SHA256SUMS ]]; then
  sha256sum --check CONTAINER_SHA256SUMS --quiet
fi
if [[ "$mode" == negative* ]]; then
  args=(--tools "$tools" --output "$out")
  if [[ "$mode" == negative ]]; then args+=(--measure); fi
  exec python3 -u scripts/fossil_nonregression.py "${args[@]}"
fi

cpus=$(python3 -c 'import os, sys; c=sorted(os.sched_getaffinity(0)); len(c)>=2 or sys.exit("At least two allowed CPUs are needed"); print(c[0],c[1])')
read -r server_cpu client_cpu <<< "$cpus"
mkdir -p "$out"
python3 scripts/collect_environment.py --output "$out/environment.json"
for preset in web dashboard; do
  lab="$out/fossil-$preset"
  common=(--lab "$lab" --preset "$preset" --cpu "$server_cpu" --client-cpu "$client_cpu"
          --before "$tools/before-fix/bin/llvm-bolt" --after "$tools/after-fix/bin/llvm-bolt")
  python3 scripts/init_fossil_reproduction.py "${common[@]}" --original "$original"
  for action in train rewrite; do
    python3 -u scripts/fossil_lab.py "$action" "${common[@]}" --concurrency 4 \
      2>&1 | tee "$out/$preset-$action.txt"
  done
  for seed in 43 44; do
    python3 -u scripts/fossil_lab.py check "${common[@]}" --concurrency 4 --seed "$seed" \
      2>&1 | tee "$out/$preset-check-seed$seed.txt"
    cp "$lab/check.json" "$lab/check-seed$seed.json"
  done
  python3 scripts/verify_mainline_sqlite.py --kind fossil --lab "$lab" --output "$out/$preset-tables"
done
printf 'PASS: fresh Fossil profiles, A/B/C HTTP content, relocated tables and NOTRACK prefixes.\n'

if [[ "$mode" == benchmark ]]; then
  python3 -u scripts/run_mainline_matrix.py --tools "$tools" --apps "$out" \
    --rounds 24 --target-seconds 3 --seeds 43 44 2>&1 | tee "$out/benchmark-console.txt"
fi
