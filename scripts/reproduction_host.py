#!/usr/bin/env python3
"""Check an unfamiliar Linux host and run the frozen experiment in Docker."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_source_files():
    for line in (ROOT / "SOURCE_SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        sha = hashlib.sha256()
        with (ROOT / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
        if sha.hexdigest() != expected:
            raise RuntimeError("Local source hash mismatch: " + name + "; restore matching release materials before running.")


def capture(argv, timeout=45):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(map(str, argv)) + "\n" + result.stderr.strip())
    return result.stdout


def busy_processes():
    found = []
    names = {"apt", "apt-get", "dpkg", "unattended-upgr", "cc1", "cc1plus", "clang", "clang++", "ld.lld"}
    for process in Path("/proc").glob("[0-9]*"):
        try:
            name = (process / "comm").read_text().strip()
            if name == "unattended-upgr" and b"unattended-upgrade-shutdown" in (process / "cmdline").read_bytes():
                continue  # This idle shutdown helper is not an active upgrade.
            if name in names and "State:\tZ" not in (process / "status").read_text():
                found.append({"pid": int(process.name), "name": name})
        except (OSError, ValueError):
            continue
    return found


def host_check(results, config):
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("Use native x86-64 Linux or x86-64 WSL2 with Linux Docker containers; ARM emulation is not supported for timing.")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        raise RuntimeError("At least two allowed CPUs are required; current affinity: " + str(allowed))
    free = shutil.disk_usage(results).free
    if free < config["minimum_free_gib"] * 1024 ** 3:
        raise RuntimeError("Keep at least 8 GiB free in the results filesystem; Docker storage needs separate room too.")
    virt = None
    if shutil.which("systemd-detect-virt"):
        proc = subprocess.run(["systemd-detect-virt"], text=True, capture_output=True, timeout=10)
        virt = proc.stdout.strip() or "unknown"
    return {"platform": platform.platform(), "machine": platform.machine(),
            "is_wsl": "microsoft" in platform.release().lower(), "virtualization": virt,
            "allowed_cpus": allowed, "results_free_bytes": free,
            "os_release": Path("/etc/os-release").read_text(),
            "meminfo": Path("/proc/meminfo").read_text(),
            "cpu_pmu_sysfs_present": Path("/sys/bus/event_source/devices/cpu").exists(),
            "pmu_note": "Sysfs presence is not a perf permission/availability test. Main experiments use BOLT instrumentation.",
            "busy_processes": busy_processes()}


def container_command(docker, image, results, argv):
    return docker + ["run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                     "--mount", f"type=bind,src={results},dst=/results", image] + argv


def preflight(args, config, results, docker):
    report = {"status": "FAIL", "collected_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "release": config["release"], "image": args.image, "results": str(results), "warnings": []}
    try:
        check_source_files()
        report["host"] = host_check(results, config)
        try:
            report["docker_server"] = json.loads(capture(docker + ["info", "--format", "{{json .}}"], 20))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Docker daemon is unavailable. Install/start Docker or use --sudo / --docker /path/to/run-docker. Do not delete apt locks.\n" + str(error)) from error
        try:
            inspected = json.loads(capture(docker + ["image", "inspect", args.image], 20))[0]
        except RuntimeError as error:
            raise RuntimeError("Load the repro-v2 image archive first with docker load. No image is pulled automatically.\n" + str(error)) from error
        report["docker_image"] = {key: inspected.get(key) for key in ("Id", "Os", "Architecture", "RootFS")}
        if inspected.get("Os") != "linux" or inspected.get("Architecture") != "amd64":
            raise RuntimeError("Expected a linux/amd64 image")
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        if labels.get("org.opencontainers.image.revision") != config["llvm_commit"]:
            raise RuntimeError("The image does not declare the pinned LLVM commit")
        # Compare the manifest itself before trusting its self-reported checks.
        expected = hashlib.sha256((ROOT / "SOURCE_SHA256SUMS").read_bytes()).hexdigest()
        command = ["bash", "-c", "sha256sum SOURCE_SHA256SUMS; sha256sum --check SOURCE_SHA256SUMS --quiet && sha256sum --check CONTAINER_SHA256SUMS --quiet"]
        verified = capture(container_command(docker, args.image, results, command), 120)
        report["source_manifest_sha256"] = expected
        if verified.split()[0] != expected:
            raise RuntimeError("Image scripts and this checkout differ. Load the matching repro-v2 delivery; the original v1 image is insufficient.")
        identity_path = getattr(args, "image_manifest", None)
        if identity_path is None and (ROOT.parent / "image-manifest.json").is_file():
            identity_path = ROOT.parent / "image-manifest.json"
        if identity_path:
            identity = json.loads(identity_path.read_text())
            if (identity.get("release") != config["release"] or
                    identity.get("source_manifest_sha256") != expected or
                    identity.get("rootfs_diff_ids") != inspected.get("RootFS", {}).get("Layers")):
                raise RuntimeError("Delivery image manifest, source manifest or RootFS layers do not match")
            report["image_identity_manifest"] = {"path": str(identity_path), "status": "PASS",
                                                 "config_digest": identity.get("config_digest")}
        else:
            report["warnings"].append("No external image-manifest.json found; source and executable checks passed, but full layer identity was not compared. Use --image-manifest from the delivery for release acceptance.")
        probe = """import json, os, subprocess, tempfile
from pathlib import Path
c=sorted(os.sched_getaffinity(0))
if len(c)<2: raise SystemExit('Container needs at least two allowed CPUs')
with tempfile.NamedTemporaryFile(dir='/results', prefix='.write-probe-'): pass
for tool in ('/opt/project/artifacts/mainline-options/common/bin/llvm-bolt', '/opt/project/artifacts/source-fossil/bin/fossil.orig'):
    subprocess.run([tool, '--version' if tool.endswith('llvm-bolt') else 'version'], check=True, stdout=subprocess.DEVNULL)
print(json.dumps({'allowed_cpus':c,'server_cpu':c[0],'client_cpu':c[1],'writable_results':True,'binary_execution':True}))
"""
        report["container_probe"] = json.loads(capture(container_command(docker, args.image, results, ["python3", "-c", probe]), 45))
        if report["host"]["busy_processes"]:
            report["warnings"].append("Package updates or compilation are active. Timing commands will stop until they finish; no process is stopped automatically.")
        if not report["host"]["cpu_pmu_sysfs_present"]:
            report["warnings"].append("No CPU PMU in sysfs. Instrumentation/timing remains usable; do not claim hardware perf, IPC or LBR results.")
        report["status"] = "PASS"
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, IndexError) as error:
        report["error"] = str(error)
    # Avoid serializing Docker's arbitrary environment/service configuration.
    if isinstance(report.get("docker_server"), dict):
        info = report["docker_server"]
        report["docker_server"] = {key: info.get(key) for key in
                                   ("ServerVersion", "OSType", "Architecture", "Driver", "DockerRootDir", "NCPU", "MemTotal")}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report["record_file"] = f"host-preflight-{stamp}.json"
    save(results / report["record_file"], report)
    save(results / "host-preflight.json", report)
    return report


def execute_stage(stage, args, results, docker, preflight_record):
    if stage in ("benchmark", "negative") and busy_processes():
        raise RuntimeError("Updates/compilation are active; wait for them to finish before timing. See /proc or ps. No system processes were modified.")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stage_dir = Path(tempfile.mkdtemp(prefix=f"{stage}-{stamp}-", dir=results))
    # The original runner creates the child and refuses to reuse existing data.
    command = container_command(docker, args.image, results,
                                ["bash", "scripts/reproduce_fossil_mainline.sh", stage] +
                                ([] if stage == "verify" else [f"/results/{stage_dir.name}/output"]))
    record = {"stage": stage, "command": command, "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "RUNNING", "source_preflight": "../" + preflight_record}
    save(stage_dir / "execution.json", record)
    start = time.monotonic()
    print("Running " + stage + "; log: " + str(stage_dir / "console.log"), flush=True)
    with (stage_dir / "console.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                code = process.wait()
            except KeyboardInterrupt:
                process.terminate()
                try:
                    code = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    code = process.wait()
                code = code or 130
    record.update(status="PASS" if code == 0 else "FAIL", exit_code=code,
                  elapsed_seconds=time.monotonic() - start,
                  finished_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    save(stage_dir / "execution.json", record)
    if code:
        raise RuntimeError(f"{stage} failed (exit {code}). Original output is preserved: {stage_dir}")


def main():
    config = json.loads((ROOT / "configs/reproduction.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["preflight", "all"] + config["stages"])
    parser.add_argument("--results", type=Path, required=True, help="A dedicated results root; each run creates a new child")
    parser.add_argument("--image", default=config["image"])
    parser.add_argument("--image-manifest", type=Path, help="Delivery manifest; defaults to image-manifest.json beside the extracted materials directory")
    parser.add_argument("--docker", default="docker", help="Docker executable or an existing daemon wrapper")
    parser.add_argument("--sudo", action="store_true", help="Invoke Docker through sudo -n; authenticate with sudo -v first")
    args = parser.parse_args()
    results = args.results.expanduser().resolve()
    results.mkdir(parents=True, exist_ok=True)
    if "," in str(results):
        parser.error("Docker --mount cannot use a results path containing a comma")
    docker = (["sudo", "-n"] if args.sudo else []) + [args.docker]
    report = preflight(args, config, results, docker)
    for warning in report["warnings"]:
        print("WARNING: " + warning, flush=True)
    if report["status"] != "PASS":
        parser.exit(1, "Preflight failed: " + report["error"] + "\nSee " + str(results / "host-preflight.json") + "\n")
    print("PASS: preflight; " + str(results / "host-preflight.json"), flush=True)
    if args.stage != "preflight":
        try:
            for stage in config["stages"] if args.stage == "all" else [args.stage]:
                execute_stage(stage, args, results, docker, report["record_file"])
        except (RuntimeError, OSError) as error:
            parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
