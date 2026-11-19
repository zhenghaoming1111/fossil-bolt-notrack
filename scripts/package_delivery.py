#!/usr/bin/env python3
"""Export an already validated image and matching sources as a new offline delivery."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new directory; existing deliveries are never replaced")
    parser.add_argument("--docker", default="docker", help="Executable or daemon wrapper")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs/reproduction.json").read_text())
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    paths = []
    for line in (ROOT / "SOURCE_SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        if digest(ROOT / name) != expected:
            raise RuntimeError("Stale source manifest: " + name)
        paths.append(ROOT / name)
    image = json.loads(subprocess.check_output([args.docker, "image", "inspect", config["image"]], text=True))[0]
    if image["Os"] != "linux" or image["Architecture"] != "amd64":
        raise RuntimeError("Expected linux/amd64")
    materials = out / config["materials_archive"]
    paths.append(ROOT / "SOURCE_SHA256SUMS")
    meta = ROOT / "release-metadata" / config["release"]
    if meta.is_dir():
        paths.extend(path for path in meta.rglob("*") if path.is_file())
    with tarfile.open(materials, "w:gz", compresslevel=6) as bundle:
        for path in sorted(set(paths)):
            bundle.add(path, arcname="fossil-bolt-submission/" + path.relative_to(ROOT).as_posix(), recursive=False)
    print("Materials exported: " + str(materials), flush=True)
    archive = out / config["image_archive"]
    with archive.open("wb") as target, gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0, compresslevel=6) as compressed:
        with subprocess.Popen([args.docker, "save", config["image"]], stdout=subprocess.PIPE) as process:
            shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
            if process.wait():
                raise RuntimeError("docker save failed; incomplete export retained for diagnosis")
    print("Image exported: " + str(archive), flush=True)
    with tarfile.open(archive, "r:gz") as bundle:
        docker_manifest = json.load(bundle.extractfile("manifest.json"))
        if len(docker_manifest) != 1:
            raise RuntimeError("Expected exactly one exported image")
        image_config_bytes = bundle.extractfile(docker_manifest[0]["Config"]).read()
        image_config = json.loads(image_config_bytes)
    manifest = {"schema": 2, "release": config["release"], "tag": config["image"],
                "platform": "linux/amd64", "llvm_commit": config["llvm_commit"],
                "reported_image_id": image["Id"],
                "config_digest": "sha256:" + hashlib.sha256(image_config_bytes).hexdigest(),
                "rootfs_diff_ids": image_config["rootfs"]["diff_ids"],
                "source_manifest_sha256": digest(ROOT / "SOURCE_SHA256SUMS"),
                "archive": archive.name, "archive_size_bytes": archive.stat().st_size,
                "archive_sha256": digest(archive), "materials": materials.name,
                "materials_sha256": digest(materials),
                "id_note": "reported_image_id may identify a manifest or config depending on Docker storage. config_digest is computed from the actual archive config; RootFS diff IDs identify the unpacked layers."}
    metadata = out / "image-manifest.json"
    metadata.write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "DELIVERY_SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in (archive, materials, metadata)))
    print("PASS: exported delivery with SHA-256 and archive-derived image identity", flush=True)


if __name__ == "__main__":
    main()
