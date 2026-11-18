#!/usr/bin/env python3
"""Test OFF/ON on a Fossil build that has no NOTRACK indirect jumps."""
import argparse
import gzip
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import struct
from types import SimpleNamespace

import fossil_lab as fossil
from collect_environment import collect


ROOT = Path(__file__).resolve().parents[1]
PLAN = {
    "scenario": "dashboard", "training_seed": 42, "evaluation_seed": 43,
    "server_concurrency": 4, "rounds": 48, "order_seed": 20261118,
    "warmup_requests": 20, "target_seconds": 1.0, "loss_margin_percent": 1.0,
    "bootstrap_resamples": 10000, "bootstrap_seed": 20261118,
}


def load(path):
    return json.loads(path.read_text())


def load_segments(path):
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"Expected a little-endian ELF64 file: {path}")
    entry, offset = struct.unpack_from("<QQ", data, 24)
    size, count = struct.unpack_from("<HH", data, 54)
    segments = []
    for index in range(count):
        typ, flags, start, vaddr, paddr, filesz, memsz, align = struct.unpack_from(
            "<II6Q", data, offset + size * index)
        if typ == 1:
            segments.append({"vaddr": vaddr, "paddr": paddr, "offset": start,
                             "filesz": filesz, "memsz": memsz, "flags": flags, "align": align,
                             "sha256": hashlib.sha256(data[start:start + filesz]).hexdigest()})
    if not segments:
        raise ValueError(f"Missing ELF load segments: {path}")
    return {"entry": entry, "segments": segments}


def summarize(rows, plan):
    if len(rows) != plan["rounds"] * 2:
        raise ValueError("Wrong number of raw rows")
    orders = [("off", "on"), ("on", "off")] * (plan["rounds"] // 2)
    random.Random(plan["order_seed"]).shuffle(orders)
    reductions = []
    counts = {row["requests"] for row in rows}
    if len(counts) != 1 or not all(row["errors"] == 0 and row["elapsed_seconds"] > 0 for row in rows):
        raise ValueError("Inconsistent work or failed requests")
    for number, order in enumerate(orders, 1):
        pair = rows[2 * (number - 1):2 * number]
        if tuple(row["variant"] for row in pair) != order or any(row["round"] != number for row in pair):
            raise ValueError(f"Incorrect run order in round {number}")
        times = {row["variant"]: row["elapsed_seconds"] for row in pair}
        reductions.append(100 * (1 - times["on"] / times["off"]))
    rng = random.Random(plan["bootstrap_seed"])
    boot = sorted(statistics.median(rng.choices(reductions, k=len(reductions)))
                  for _ in range(plan["bootstrap_resamples"]))
    interval = [boot[len(boot) // 40], boot[len(boot) * 39 // 40 - 1]]
    return {"status": "COMPLETE", "rounds": plan["rounds"], "records": len(rows),
            "requests_per_run": counts.pop(), "http_errors": 0,
            "elapsed_median_seconds": {label: statistics.median(
                row["elapsed_seconds"] for row in rows if row["variant"] == label) for label in ("off", "on")},
            "paired_median_reduction_percent": statistics.median(reductions),
            "paired_bootstrap_median_interval_95": interval,
            "wins_ties_losses": [sum(x > 0 for x in reductions), sum(x == 0 for x in reductions),
                                 sum(x < 0 for x in reductions)],
            "noninferiority_passed": interval[0] > -plan["loss_margin_percent"]}


def run(out, tools, run_timing):
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        raise RuntimeError("Allow at least two CPUs for the server and the client")
    plan = {**PLAN, "server_cpu": allowed[0], "client_cpu": allowed[1]}
    out.mkdir(parents=True, exist_ok=False)
    for path in ("bin", "src", "profiles", "results", "logs", "data/traces"):
        (out / path).mkdir(parents=True, exist_ok=True)
    fossil.save(out / "analysis-plan.json", plan)
    fossil.save(out / "environment.json", collect())
    original = out / "bin/fossil.orig"
    source = ROOT / "datasets/fossil-src-2.28.tar.gz"
    snapshot = ROOT / "datasets/fossil-snapshot.fossil.gz"
    data_manifest = load(ROOT / "datasets/manifest.json")
    inputs = [source, snapshot] + [ROOT / f"datasets/traces/dashboard-seed{seed}.json" for seed in (42, 43)]
    for path in inputs:
        if fossil.digest(path) != data_manifest["files_sha256"][path.relative_to(ROOT).as_posix()]:
            raise RuntimeError(f"Input hash mismatch: {path}")
    for path in inputs[2:]:
        shutil.copy2(path, out / "data/traces" / path.name)
    with gzip.open(snapshot, "rb") as stream, (out / "data/snapshot.fossil").open("wb") as target:
        shutil.copyfileobj(stream, target)
    if fossil.digest(out / "data/snapshot.fossil") != data_manifest["snapshot_sha256"]:
        raise RuntimeError("Incorrect uncompressed repository snapshot")
    fossil.run(out, "extract", ["tar", "-xzf", source, "--strip-components=1", "-C", out / "src"])
    cflags = "-O2 -g -fno-omit-frame-pointer -fno-reorder-blocks-and-partition -fno-pie -no-pie -fcf-protection=none"
    ldflags = "-no-pie -Wl,--emit-relocs"
    env = dict(os.environ, CC="gcc", CFLAGS=cflags, LDFLAGS=ldflags)
    print("Build the frozen Fossil sources with -fcf-protection=none", flush=True)
    fossil.run(out, "configure", ["./configure", "--disable-fusefs"], cwd=out / "src", env=env)
    fossil.run(out, "make", ["make", "-j4"], cwd=out / "src", env=env, timeout=1800)
    shutil.copy2(out / "src/fossil", original)
    assembly = fossil.run(out, "objdump", ["objdump", "-d", "-w", original])
    if re.search(r"\b(?:notrack|ds)\s+jmp(?:q)?\b", assembly):
        raise RuntimeError("NOTRACK jump found in the purported negative build")
    print("PASS: whole-executable disassembly has no NOTRACK JMP", flush=True)
    paths = {label: tools / subpath for label, subpath in (
        ("before", "before-fix/bin/llvm-bolt"), ("after", "after-fix/bin/llvm-bolt"))}
    manifest = load(tools / "manifest.json")
    if not manifest["both_modes_use_same_executable"] or any(
            fossil.digest(tools / path) != expected for path, expected in manifest["files"].items()):
        raise RuntimeError("Unexpected BOLT toolchain")
    args = SimpleNamespace(lab=out, cpu=plan["server_cpu"], client_cpu=plan["client_cpu"],
                           concurrency=plan["server_concurrency"], seed=plan["evaluation_seed"], **paths)
    fossil.MIXES = fossil.DASHBOARD_MIXES
    fossil.train(args)
    profile = out / "profiles/fossil.fdata"
    rewrites = {}
    for label, tool, filename in (("off", args.before, "fossil.before"), ("on", args.after, "fossil.after")):
        binary = out / "bin" / filename
        fossil.run(out, label + "-rewrite", [tool, original, f"-data={profile}", "-o", binary,
                   "-reorder-blocks=ext-tsp", "-reorder-functions=hfsort", "-split-functions=false",
                   "-jump-tables=move", "-no-threads", "-sequential-disassembly"])
        rewrites[label] = {"elf_sha256": fossil.digest(binary), "image": load_segments(binary)}
    if rewrites["off"]["image"] != rewrites["on"]["image"]:
        raise RuntimeError("OFF and ON load segments differ on nonmatching input")
    fossil.check(args)
    fossil.save(out / "correctness.json", {"status": "PASS", "no_notrack_jmp": True,
                "load_segments_identical": True, "compiler_flags": cflags, "linker_flags": ldflags,
                "original_sha256": fossil.digest(original), "profile_sha256": fossil.digest(profile),
                "fossil_version": fossil.run(out, "version", [original, "version", "-v"]),
                "snapshot_sha256": data_manifest["snapshot_sha256"], "rewrites": rewrites})
    print("PASS: identical ELF load segments and visible A/OFF/ON pages", flush=True)
    if not run_timing:
        return

    requests = fossil.trace(args, plan["scenario"], args.seed)
    warmup = requests[:plan["warmup_requests"]]
    binaries = {"off": out / "bin/fossil.before", "on": out / "bin/fossil.after"}
    with fossil.Server(args, binaries["off"], "off-pilot") as server:
        fossil.replay(server, warmup, concurrency=args.concurrency)
        pilot = fossil.replay(server, requests, concurrency=args.concurrency)
    repeats = math.ceil(plan["target_seconds"] / pilot["elapsed_seconds"])
    fossil.save(out / "calibration.json", {"pilot": pilot, "repeats": repeats})
    orders = list(itertools.permutations(binaries)) * (plan["rounds"] // 2)
    random.Random(plan["order_seed"]).shuffle(orders)
    rows = []
    with (out / "raw.jsonl").open("x") as raw:
        for number, order in enumerate(orders, 1):
            for label in order:
                with fossil.Server(args, binaries[label], f"measure-{number:02d}-{label}") as server:
                    fossil.replay(server, warmup, concurrency=args.concurrency)
                    row = fossil.replay(server, requests, repeats, args.concurrency)
                row.update(round=number, variant=label)
                rows.append(row)
                raw.write(json.dumps(row) + "\n")
                raw.flush()
            print(f"Negative round {number}/{plan['rounds']}", flush=True)
    summary = summarize(rows, plan)
    fossil.save(out / "summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)
    if not summary["noninferiority_passed"]:
        raise RuntimeError("This measurement did not establish noninferiority at the preset margin")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tools", type=Path, default=ROOT / "artifacts/mainline-options")
    parser.add_argument("--measure", action="store_true", help="Also run all 48 formal pairs")
    args = parser.parse_args()
    run(args.output.resolve(), args.tools.resolve(), args.measure)


if __name__ == "__main__":
    main()
