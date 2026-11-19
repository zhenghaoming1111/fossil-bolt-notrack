#!/usr/bin/env python3
"""Refresh or check the source manifest without changing archived experiment data."""
import argparse
import hashlib
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", "delivery", "artifacts", "build", "results", "release-metadata", "__pycache__"}


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def manifest():
    listed = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    names = sorted(set(name.decode("utf-8") for name in listed.split(b"\0") if name))
    return "".join(f"{digest(ROOT / name)}  ./{name}\n" for name in names
                   if name != "SOURCE_SHA256SUMS" and not set(Path(name).parts) & EXCLUDED
                   and not name.endswith(".pyc") and (ROOT / name).is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "SOURCE_SHA256SUMS"
    expected = manifest()
    if args.check:
        if path.read_text(encoding="utf-8") != expected:
            parser.exit(1, "SOURCE_SHA256SUMS is stale; run scripts/update_source_checksums.py after reviewing the changes.\n")
        print("PASS: source manifest")
    else:
        path.write_text(expected, encoding="utf-8", newline="\n")
        print(f"Updated {path.name}: {len(expected.splitlines())} files")


if __name__ == "__main__":
    main()
