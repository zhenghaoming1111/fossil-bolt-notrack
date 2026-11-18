#!/usr/bin/env bash
# Build the frozen mainline and the opt-in NOTRACK recognition feature.
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
config="$root/configs/llvm-mainline-options.json"
commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["upstream_commit"])' "$config")
archive_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_archive_sha256"])' "$config")
work=${BOLT_BUILD_ROOT:-"$root/build/llvm-options-${commit:0:12}"}
dest=${BOLT_TOOLS_ROOT:-"$root/artifacts/mainline-options-${commit:0:12}"}
jobs=${BUILD_JOBS:-4}
tests=${RUN_BOLT_TESTS:-1}
src="$work/src/llvm-project-$commit"
build="$work/build"
archive="$work/downloads/llvm-project-$commit.tar.gz"
patch="$root/patches/notrack-jump-table-options.patch"
if [[ -e "$dest" ]]; then printf 'Preserving existing tools; choose a new BOLT_TOOLS_ROOT.\n' >&2; exit 1; fi
mkdir -p "$work/downloads" "$work/src" "$work/logs"
if [[ ! -f "$archive" ]]; then
  curl -4 --fail --location --retry 3 --connect-timeout 20 --max-time 1800 \
    "https://codeload.github.com/llvm/llvm-project/tar.gz/$commit" -o "$archive.part"
  printf '%s  %s\n' "$archive_sha" "$archive.part" | sha256sum --check
  mv -- "$archive.part" "$archive"
fi
python3 "$root/scripts/verify_mainline_patch.py" --config "$config" --patch "$patch" \
  --archive "$archive" --report "$work/logs/patch-check.json"
if [[ ! -d "$src" ]]; then tar -xzf "$archive" -C "$work/src"; fi
patch --directory="$src" -p1 --fuzz=0 --dry-run -i "$patch"
patch --directory="$src" -p1 --fuzz=0 -i "$patch"
projects=bolt
extra=()
targets=(llvm-bolt bolt_rt llvm-objdump llvm-mc FileCheck)
if [[ "$tests" == 1 ]]; then
  python3 -c 'import psutil'
  projects='clang;lld;bolt'
  extra=(-DBOLT_CLANG_EXE="$build/bin/clang" -DBOLT_LLD_EXE="$build/bin/ld.lld" -DCLANG_INCLUDE_TESTS=OFF)
  targets+=(clang lld clang-resource-headers bolt-test-depends)
fi
cmake -G Ninja -S "$src/llvm" -B "$build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER="${CC:-clang-18}" \
  -DCMAKE_CXX_COMPILER="${CXX:-clang++-18}" -DLLVM_USE_LINKER=lld-18 \
  -DLLVM_ENABLE_PROJECTS="$projects" -DLLVM_TARGETS_TO_BUILD=X86 \
  -DLLVM_ENABLE_ASSERTIONS=ON -DLLVM_INCLUDE_TESTS=ON -DLLVM_PARALLEL_LINK_JOBS=1 \
  -DLLVM_APPEND_VC_REV=ON -DLLVM_FORCE_VC_REVISION="$commit" \
  -DLLVM_FORCE_VC_REPOSITORY=https://github.com/llvm/llvm-project "${extra[@]}" \
  2>&1 | tee "$work/logs/configure.txt"
cmake --build "$build" --target "${targets[@]}" --parallel "$jobs" \
  2>&1 | tee "$work/logs/build.txt"
python3 "$root/scripts/install_option_toolchain.py" --build "$build" --output "$dest" --config "$config"
"$dest/common/bin/llvm-bolt" --version | tee "$work/logs/version.txt"
if [[ "$tests" == 1 ]]; then
  python3 "$build/bin/llvm-lit" -v -j "$jobs" -o "$work/logs/lit-options-all.json" \
    "$src/bolt/test" 2>&1 | tee "$work/logs/lit-options-all.txt"
fi
python3 "$root/scripts/verify_mainline_fixture.py" --tools "$dest" --cc "${CC:-clang-18}" \
  --output "$work/fixture"
printf 'Same executable, OFF/ON launchers: %s\n' "$dest"
