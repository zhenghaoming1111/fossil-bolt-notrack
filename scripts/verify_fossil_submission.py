#!/usr/bin/env python3
"""Recompute the Fossil-only results from the archived round logs."""
from collections import Counter
from pathlib import Path
import gzip
import itertools
import json
import random
import re

from fossil_lab import VARIANTS, digest, paired_summary
from fossil_nonregression import PLAN, summarize as summarize_negative


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "mainline-options" / "af3fdf39125a"


def load(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    config = load(ROOT / "configs/llvm-mainline-options.json")
    require(digest(ROOT / config["patch"]) == config["patch_sha256"], "Unexpected patch")
    require(load(EVIDENCE / "patch-check.json")["reverse_restores_original"], "Patch check did not pass")
    data = load(ROOT / "datasets/manifest.json")
    for filename, expected in data["files_sha256"].items():
        require(digest(ROOT / filename) == expected, f"Unexpected input: {filename}")
    frozen = data["frozen_script_sha256"]
    require(digest(ROOT / "scripts/fossil_lab.py") == frozen, "Unexpected benchmark script")
    plan = load(EVIDENCE / "formal-analysis-plan.json")
    tools = load(EVIDENCE / "toolchain-manifest.json")
    require(plan["actual_tool_manifest"] == tools and tools["both_modes_use_same_executable"], "Unexpected BOLT")
    require(plan["rounds"] == 24 and plan["seeds"] == [43, 44], "Unexpected formal plan")
    lit = load(EVIDENCE / "logs/lit-options-all.json")
    counts = Counter(test["code"] for test in lit["tests"])
    require(counts == {"PASS": 527, "UNSUPPORTED": 267, "XFAIL": 1}, f"Unexpected BOLT regression counts: {counts}")

    total = verified_pages = 0
    for preset, cases in (("web", {"browse", "review"}), ("dashboard", {"dashboard"})):
        app = EVIDENCE / "apps" / ("fossil-" + preset)
        training, rewrite = load(app / "training.json"), load(app / "rewrites.json")
        require(digest(app / "fossil.fdata") == training["profile_sha256"] == rewrite["profile_sha256"], "Unexpected profile")
        require(all(x["nonzero_records"] > 0 for x in training["targets"].values()), "Missing hot counters")
        require(load(app / "tables/summary.json")["passed"], "Table/prefix checks did not pass")
        for seed in plan["seeds"]:
            run = EVIDENCE / "positive" / preset / ("seed" + str(seed))
            checked, report = load(run / "check.json"), load(run / "summary.json")
            require(checked["status"] == "PASS" and report["status"] == "COMPLETE" and
                    checked["seed"] == report["evaluation_seed"] == seed, "Incorrect seed or unfinished verification")
            require(report["script_sha256"] == frozen and report["binary_sha256"] == checked["binary_sha256"], "Unexpected input")
            require(report["trace_sha256"] == checked["trace_sha256"] == {
                case: data["files_sha256"][f"datasets/traces/{case}-seed{seed}.json"] for case in cases}, "Unexpected traces")
            require(report["dataset_sha256"] == checked["snapshot_sha256"] == data["snapshot_sha256"], "Unexpected snapshot")
            require(report["profile_sha256"] == training["profile_sha256"], "Unexpected training profile")
            require(report["binary_sha256"] == {
                "A-original": training["original_sha256"],
                "B-before-fix": rewrite["variants"]["before"]["binary_sha256"],
                "C-after-fix": rewrite["variants"]["after"]["binary_sha256"]}, "Unexpected rewritten variant")
            raw = rows(run / "raw.jsonl")
            require(len(raw) == len(cases) * 72 and set(report["cases"]) == cases, "Unexpected number of runs")
            for case in sorted(cases):
                item = report["cases"][case]
                actual = [{key: val for key, val in row.items() if key != "case"} for row in raw if row["case"] == case]
                require(actual == item["rows"], "Raw rows differ from recorded summary input")
                orders = [list(row) for row in itertools.permutations(VARIANTS)] * 4
                random.Random(plan["order_seed"]).shuffle(orders)
                for i, order in enumerate(orders):
                    require([row["variant"] for row in actual[i*3:i*3+3]] == order and
                            all(row["round"] == i+1 for row in actual[i*3:i*3+3]), "Unbalanced or incomplete rounds")
                require(all(row["requests"] == item["requests_per_measured_run"] and row["errors"] == 0
                            for row in actual), "Unequal or failed requests")
                ref = checked["variants"]["A-original"][case]["semantic_sha256"]
                require(len(ref) == 100, "Wrong number of checked pages")
                for label in VARIANTS:
                    require(checked["variants"][label][case]["semantic_sha256"] == ref, "Page mismatch")
                    verified_pages += len(ref)
                result = paired_summary(actual)
                require(result == item["summary"], f"Statistics differ: {case}/seed{seed}")
                cmp = result["comparisons"]["B-before-fix -> C-after-fix"]
                print(f"{case:10s} seed{seed}: {cmp['elapsed_reduction_paired_median_percent']:+.3f}% OFF -> ON")
            total += len(raw)

    neg = EVIDENCE / "fossil-nonmatching"
    plan = load(neg / "analysis-plan.json")
    require(all(plan.get(key) == val for key, val in PLAN.items()), "Unexpected negative test plan")
    correctness = load(neg / "correctness.json")
    require(correctness["status"] == "PASS" and correctness["no_notrack_jmp"] and correctness["load_segments_identical"], "Negative checks failed")
    require(correctness["rewrites"]["off"]["image"] == correctness["rewrites"]["on"]["image"], "Negative load segments differ")
    with gzip.open(neg / "logs/objdump.stdout.txt.gz", "rt") as asm:
        require(not any(re.search(r"\b(?:notrack|ds)\s+jmp(?:q)?\b", line) for line in asm),
                "NOTRACK jump found in negative disassembly")
    checked = load(neg / "check.json")
    pages = [checked["variants"][group]["dashboard"]["semantic_sha256"] for group in VARIANTS]
    require(checked["status"] == "PASS" and len(pages[0]) == 100 and pages[0] == pages[1] == pages[2], "Negative page mismatch")
    raw = rows(neg / "raw.jsonl")
    summary = summarize_negative(raw, plan)
    require(summary == load(neg / "summary.json"), "Negative statistics differ")
    print(f"nonmatching: {summary['paired_median_reduction_percent']:+.3f}%, "
          f"95% interval {summary['paired_bootstrap_median_interval_95']}, "
          f"below 1% regression margin: {summary['noninferiority_passed']}")
    print(f"PASS: recomputed {total} positive + {len(raw)} negative timing rows; "
          f"verified {verified_pages} positive page digests; BOLT regression {dict(counts)}")


if __name__ == "__main__":
    main()
