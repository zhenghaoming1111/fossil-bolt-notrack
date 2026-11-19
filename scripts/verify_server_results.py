#!/usr/bin/env python3
"""Recompute the 2026-11-19 cross-machine Fossil evidence, without running binaries."""
from collections import Counter
import itertools
import json
from pathlib import Path
import random

from fossil_lab import VARIANTS, digest, paired_summary
from fossil_nonregression import PLAN, summarize

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence/server-20261119"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main():
    plan = load(EVIDENCE / "formal-analysis-plan.json")
    require(plan["rounds"] == 24 and plan["seeds"] == [43, 44] and plan["training_seed"] == 42, "Unexpected formal plan")
    require(plan["actual_tool_manifest"] == load(ROOT / "evidence/mainline-options/af3fdf39125a/toolchain-manifest.json"), "Unexpected BOLT")
    dataset = load(ROOT / "datasets/manifest.json")
    total = pages = 0
    for preset, cases in (("web", {"browse", "review"}), ("dashboard", {"dashboard"})):
        trained = load(EVIDENCE / "apps" / preset / "training.json")
        require(digest(EVIDENCE / "apps" / preset / "fossil.fdata") == trained["profile_sha256"], "Profile hash mismatch")
        require(all(x["nonzero_records"] > 0 for x in trained["targets"].values()), "Missing hot counters")
        require(load(EVIDENCE / "apps" / preset / "tables.json")["passed"], "Table check failed")
        for seed in plan["seeds"]:
            directory = EVIDENCE / "positive" / preset / f"seed{seed}"
            report, checked = load(directory / "summary.json"), load(directory / "check.json")
            raw = rows(directory / "raw.jsonl")
            require(len(raw) == len(cases) * 72 and set(report["cases"]) == cases, "Wrong row count/scenarios")
            require(report["evaluation_seed"] == checked["seed"] == seed, "Wrong seed")
            require(report["profile_sha256"] == trained["profile_sha256"], "Profile differs")
            require(report["dataset_sha256"] == dataset["snapshot_sha256"], "Snapshot differs")
            require(report["script_sha256"] == dataset["frozen_script_sha256"], "Request driver differs")
            for case in sorted(cases):
                actual = [{key: value for key, value in row.items() if key != "case"} for row in raw if row["case"] == case]
                item = report["cases"][case]
                require(actual == item["rows"], "Summary input differs")
                orders = list(itertools.permutations(VARIANTS)) * 4
                random.Random(plan["order_seed"]).shuffle(orders)
                for index, order in enumerate(orders):
                    batch = actual[index * 3:index * 3 + 3]
                    require(tuple(row["variant"] for row in batch) == order and all(row["round"] == index + 1 for row in batch), "Unbalanced rounds")
                require(all(row["requests"] == item["requests_per_measured_run"] and row["errors"] == 0 for row in actual), "Request mismatch/error")
                reference = checked["variants"]["A-original"][case]["semantic_sha256"]
                require(len(reference) == 100 and checked["status"] == "PASS", "Missing page checks")
                for label in VARIANTS:
                    require(checked["variants"][label][case]["semantic_sha256"] == reference, "Page mismatch")
                    pages += len(reference)
                recomputed = paired_summary(actual)
                require(recomputed == item["summary"], "Statistics mismatch")
                delta = recomputed["comparisons"]["B-before-fix -> C-after-fix"]["elapsed_reduction_paired_median_percent"]
                print(f"server {case:10s} seed{seed}: {delta:+.3f}% OFF -> ON")
            total += len(raw)
    negative = EVIDENCE / "negative"
    negative_plan = load(negative / "analysis-plan.json")
    require(all(negative_plan[key] == val for key, val in PLAN.items()), "Negative plan differs")
    raw = rows(negative / "raw.jsonl")
    require(summarize(raw, negative_plan) == load(negative / "summary.json"), "Negative statistics mismatch")
    audit = load(negative / "runtime-equivalence.json")
    require(audit["runtime_image_equivalent"] and audit["normalized_images"]["off"] == audit["normalized_images"]["on"], "Negative runtime audit failed")
    require(not audit["raw_load_segments_identical"] and all(40 <= i < 48 for i in audit["actual_loaded_differing_offsets"]), "Unexpected original failure cause")
    require(total == 432 and len(raw) == 96 and pages == 1800, "Missing records")
    print(f"PASS: server {total} positive + {len(raw)} negative rows and {pages} page digests; original failed-stage record retained")


if __name__ == "__main__":
    main()
