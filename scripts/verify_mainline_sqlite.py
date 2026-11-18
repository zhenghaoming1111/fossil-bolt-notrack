#!/usr/bin/env python3
"""Inspect SQLite target functions in a freshly verified food or Fossil lab."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from verify_mainline_fixture import read_table

TARGETS = {"sqlite3VdbeExec": 2, "vdbeRecordCompareInt": 1}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cfg(text):
    states = {}
    for match in re.finditer(r'Binary Function "([^"\n]+)" after building cfg \{(.*?)End of Function "[^"\n]+"', text, re.S):
        symbol = match[1].split("/", 1)[0]
        if symbol not in TARGETS:
            continue
        simple = re.search(r"IsSimple\s*:\s*(\d+)", match[2])
        tables = [{"original_address": int(table[1], 16), "entry_count": len(table[2].splitlines())}
                  for table in re.finditer(r"Jump table .*? at 0x([0-9a-f]+) with a total count of \d+:\n((?:  0x[^\n]+\n)+)", match[2])]
        states[symbol] = {"is_simple": int(simple[1]) if simple else None,
                          "jump_table_markers": match[2].count("JUMPTABLE"),
                          "tables": sorted(tables, key=lambda t: t["original_address"])}
    return states


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", type=Path, required=True)
    parser.add_argument("--kind", choices=["food", "fossil"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lab = args.lab.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "kind": args.kind, "lab": str(lab),
              "formal_performance_measurements": False, "variants": {}}
    if args.kind == "food":
        manifest = json.loads((lab / "manifest.json").read_text())
        binaries = {name: lab / value["path"] for name, value in manifest["variants"].items()}
        logs = {"B-before-fix": lab / "logs/B-before-fix-rewrite.stdout.txt",
                "C-after-fix": lab / "logs/C-after-fix-rewrite.stdout.txt"}
        profile = lab / "profiles/food.fdata"
        report["pipeline_expected_verification"] = manifest["expected_verification"]
    else:
        checked = json.loads((lab / "check.json").read_text())
        if checked["status"] != "PASS":
            raise RuntimeError("Fossil lab has not passed content verification")
        binaries = {"A-original": lab / "bin/fossil.orig", "B-before-fix": lab / "bin/fossil.before", "C-after-fix": lab / "bin/fossil.after"}
        logs = {"B-before-fix": lab / "logs/before-rewrite.stdout.txt", "C-after-fix": lab / "logs/after-rewrite.stdout.txt"}
        profile = lab / "profiles/fossil.fdata"
        report["http_verification"] = {"status": checked["status"], "seed": checked["seed"]}
    profile_records = profile.read_text().splitlines()
    counts = {target: sum(1 for line in profile_records if line.split() and line.split()[-1].isdigit()
                         and int(line.split()[-1]) > 0 and any(p.split("/")[0] == target for p in line.split()))
              for target in TARGETS}
    if not all(counts.values()):
        raise RuntimeError("Target lacks fresh nonzero training records")
    report.update(profile_sha256=digest(profile), target_nonzero_records=counts)

    def inspect(binary, symbol, label):
        command = ["objdump", "-d", "-w", "-Mintel", "--disassemble=" + symbol, str(binary)]
        text = subprocess.check_output(command, text=True)
        (output / (label + "." + symbol + ".objdump.txt")).write_text(text)
        header = re.search(r"^([0-9a-f]+) <" + re.escape(symbol) + r">:", text, re.M)
        if not header:
            raise RuntimeError("Missing live function symbol: " + symbol)
        end = start = int(header[1], 16)
        jumps = []
        for line in text.splitlines():
            instruction = re.match(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s)+)\s*(.*)", line)
            if not instruction:
                continue
            raw = bytes.fromhex(instruction[2])
            address = int(instruction[1], 16)
            end = max(end, address + len(raw))
            jump = re.search(r"notrack jmp\s+QWORD PTR \[\w+\*8\+0x([0-9a-f]+)\]", instruction[3])
            if jump:
                if raw[:2] != b"\x3e\xff":
                    raise RuntimeError("NOTRACK prefix changed")
                jumps.append({"instruction_address": address, "table_address": int(jump[1], 16), "instruction_bytes": raw.hex(" ")})
        if len(jumps) != TARGETS[symbol]:
            raise RuntimeError("Unexpected target NOTRACK count: " + label + "/" + symbol)
        return {"function_start": start, "function_end": end, "jumps": jumps}

    try:
        original = {name: inspect(binaries["A-original"], name, "A-original") for name in TARGETS}
        for label, binary in binaries.items():
            states = cfg(logs[label].read_text()) if label in logs else None
            code = {name: inspect(binary, name, label) for name in TARGETS} if states else original
            for name, expected_tables in TARGETS.items():
                if states and states.get(name, {}).get("is_simple") != int(label == "C-after-fix"):
                    raise RuntimeError("Unexpected target recognition: " + label + "/" + name)
                if label != "C-after-fix":
                    continue
                if states[name]["jump_table_markers"] != expected_tables or len(states[name]["tables"]) != expected_tables:
                    raise RuntimeError("Expected recovered tables absent")
                current = code[name]
                if current["function_start"] == original[name]["function_start"]:
                    raise RuntimeError("Target was not relocated")
                # In move mode with splitting disabled, emitted tables retain
                # the original per-function address order.
                for jump, table in zip(sorted(current["jumps"], key=lambda j: j["table_address"]), states[name]["tables"]):
                    entries = read_table(binary, jump["table_address"], table["entry_count"])
                    if not all(current["function_start"] <= value < current["function_end"] for value in entries):
                        raise RuntimeError("Stale or out-of-function SQLite jump-table entry")
                    jump.update(entry_count=len(entries), table_entries=entries, all_targets_within_function=True)
            report["variants"][label] = {"binary_sha256": digest(binary), "cfg": states, "code": code}
        report["passed"] = True
    finally:
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: SQLite recognition, fresh counters, relocated functions, prefixes and table entries.")


if __name__ == "__main__":
    main()
