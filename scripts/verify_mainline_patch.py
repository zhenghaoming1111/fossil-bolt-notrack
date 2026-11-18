#!/usr/bin/env python3
"""Check the mainline patch against the distributed commit/archive manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BUILDER = "bolt/lib/Target/X86/X86MCPlusBuilder.cpp"
NEW_FILES = ("bolt/test/X86/notrack-jump-table.test",
             "bolt/test/X86/Inputs/notrack-jump-table.c",
             "bolt/test/X86/Inputs/notrack-jump-table.s")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/llvm-mainline.json")
    parser.add_argument("--patch", type=Path, default=ROOT / "patches/notrack-jump-table-mainline.patch")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if digest(args.archive) != config["source_archive_sha256"]:
        parser.error("Archive differs from the pinned mainline manifest")
    prefix = "llvm-project-" + config["upstream_commit"] + "/"
    original = None
    with tarfile.open(args.archive, "r|gz") as archive:
        for member in archive:
            if not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):]
            if relative in NEW_FILES:
                raise RuntimeError("The regression fixture already exists in the selected upstream")
            if relative == BUILDER:
                if not member.isfile():
                    raise RuntimeError("The upstream builder is not a regular file")
                original = archive.extractfile(member).read()
            if original is not None and not relative.startswith("bolt/"):
                break
    if original is None or hashlib.sha256(original).hexdigest() != config["baseline_file_sha256"]:
        raise RuntimeError("Pinned mainline source file not found or checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="bolt-mainline-patch-check-") as temporary:
        clean = Path(temporary)
        target = clean / BUILDER
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        patch = args.patch.resolve()
        for command in (["git", "apply", "--check", str(patch)],
                        ["git", "apply", str(patch)]):
            subprocess.run(command, cwd=clean, check=True)
        files = {name: digest(clean / name) for name in (BUILDER, *NEW_FILES)}
        if files[BUILDER] != config["patched_file_sha256"]:
            raise RuntimeError("Patched source does not match the tested implementation")
        subprocess.run(["git", "apply", "--check", "--reverse", str(patch)], cwd=clean, check=True)
        subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=clean, check=True)
        if target.read_bytes() != original or any((clean / name).exists() for name in NEW_FILES):
            raise RuntimeError("Reverse application did not restore the clean source")
    report = {"passed": True, "base_commit": config["upstream_commit"],
              "source_archive_sha256": digest(args.archive), "patch_sha256": digest(args.patch),
              "forward_apply_checked": True, "reverse_apply_checked": True,
              "reverse_restores_original": True, "files_after_patch": files}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: pinned mainline, clean patch application and exact reversal.")


if __name__ == "__main__":
    main()
