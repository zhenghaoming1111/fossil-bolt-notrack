#!/usr/bin/env python3
"""Check upstream/patched BOLT recognition, instrumentation and table relocation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("dispatch_plain", "dispatch_notrack", "dispatch_fs", "dispatch_gs")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cfg_states(text):
    states = {}
    for symbol in SYMBOLS:
        match = re.search(r'Binary Function "' + re.escape(symbol) +
                          r'" after building cfg \{(.*?)(?=\nBinary Function |\Z)', text, re.S)
        if not match:
            raise RuntimeError("Missing function CFG: " + symbol)
        state = re.search(r"IsSimple\s*:\s*(\d+)", match.group(1))
        if not state:
            raise RuntimeError("Missing IsSimple: " + symbol)
        states[symbol] = {"is_simple": int(state.group(1)),
                          "jump_table_markers": match.group(1).count("JUMPTABLE")}
    return states


def read_table(binary, address, count):
    data = Path(binary).read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise RuntimeError("Expected little-endian ELF64")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    for index in range(header[10]):
        segment = struct.unpack_from("<IIQQQQQQ", data, header[5] + index * header[9])
        if segment[0] == 1 and segment[3] <= address and address + 8 * count <= segment[3] + segment[5]:
            return list(struct.unpack_from("<" + "Q" * count, data, segment[2] + address - segment[3]))
    raise RuntimeError("Table outside file-backed ELF LOAD segments")


def inspect_notrack(text, binary, *, original_start=None):
    header = re.search(r"^([0-9a-f]+) <dispatch_notrack>:", text, re.M)
    if not header:
        raise RuntimeError("Missing live dispatch_notrack symbol")
    start = int(header[1], 16)
    end = start
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
                raise RuntimeError("NOTRACK prefix encoding changed")
            table_address = int(jump[1], 16)
            jumps.append({"instruction_address": address, "table_address": table_address,
                          "instruction_bytes": raw.hex(" "),
                          "table_entries": read_table(binary, table_address, 3)})
    if len(jumps) != 1:
        raise RuntimeError("Expected exactly one NOTRACK pointer table")
    if not all(start <= target < end for target in jumps[0]["table_entries"]):
        raise RuntimeError("Stale or out-of-function table target")
    if original_start is not None and start == original_start:
        raise RuntimeError("Patched output function was not relocated")
    return {"function_start": start, "function_end": end, "jumps": jumps,
            "prefix_retained": True, "all_table_targets_within_function": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cc", default="clang-18")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Run under Linux")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    tools = args.tools.resolve()
    report = {"passed": False, "scope": "Finite correctness checks; no performance or enforced IBT claims", "variants": {}}

    def run(label, command):
        command = [str(item) for item in command]
        (output / "logs" / (label + ".command.json")).write_text(json.dumps(command, indent=2) + "\n")
        proc = subprocess.run(command, capture_output=True, text=True, timeout=180)
        text = proc.stdout + proc.stderr
        (output / "logs" / (label + ".txt")).write_text(text)
        if proc.returncode:
            raise RuntimeError(label + " failed: " + text[-3000:])
        print("PASS:", label, flush=True)
        return text

    def disassembly(label, binary):
        return run(label, ["objdump", "-d", "-w", "-Mintel", "--disassemble=dispatch_notrack", binary])

    try:
        original = output / "original"
        run("build", [args.cc, "-O2", "-g", "-fno-pie", "-no-pie", "-Wl,-q",
                      args.root / "tests/fixture/main.c", args.root / "tests/fixture/dispatch.S", "-o", original])
        run("original", [original])
        original_code = inspect_notrack(disassembly("original-code", original), original)
        report["original"] = {"sha256": sha256(original), "code": original_code}
        common = ["--lite=0", "--jump-tables=move", "--split-functions=false",
                  "--no-threads", "--sequential-disassembly"]
        for name in ("before-fix", "after-fix"):
            bolt = tools / name / "bin/llvm-bolt"
            rewritten = output / name
            text = run(name + "-rewrite", [bolt, original, "-o", rewritten, *common,
                       "--reorder-blocks=reverse", "--print-cfg", "--print-only=" + ",".join(SYMBOLS)])
            states = cfg_states(text)
            expected = dict(dispatch_plain=1, dispatch_notrack=int(name == "after-fix"), dispatch_fs=0, dispatch_gs=0)
            if {key: value["is_simple"] for key, value in states.items()} != expected:
                raise RuntimeError("Recognition differs from expected upstream/patched behavior: " + repr(states))
            if name == "after-fix" and any(states[key]["jump_table_markers"] != 1 for key in SYMBOLS[:2]):
                raise RuntimeError("Missing recovered jump table")
            run(name + "-execute", [rewritten])
            code = inspect_notrack(disassembly(name + "-code", rewritten), rewritten,
                                   original_start=original_code["function_start"] if name == "after-fix" else None)
            report["variants"][name] = {"bolt_sha256": sha256(bolt), "binary_sha256": sha256(rewritten), "cfg": states, "code": code}
        after = tools / "after-fix/bin/llvm-bolt"
        profile = output / "fixture.fdata"
        instrumented = output / "instrumented"
        run("instrument", [after, original, "--instrument", "--runtime-instrumentation-lib=libbolt_rt_instr.a",
                           "--instrumentation-file=" + str(profile), "-o", instrumented])
        run("instrumented-execute", [instrumented])
        # Instrumentation may insert counters between original labels; inspect
        # all table targets and the emitted prefix in the new live function.
        inst_code = inspect_notrack(disassembly("instrumented-code", instrumented), instrumented,
                                    original_start=original_code["function_start"])
        if not profile.is_file() or not profile.stat().st_size:
            raise RuntimeError("No fresh instrumentation profile")
        records = [line for line in profile.read_text().splitlines()
                   if any(item.split("/")[0] == "dispatch_notrack" for item in line.split())
                   and line.split()[-1].isdigit() and int(line.split()[-1]) > 0]
        if not records:
            raise RuntimeError("No nonzero NOTRACK training records")
        profiled = output / "profiled"
        run("profiled-rewrite", [after, original, "--data=" + str(profile), "-o", profiled,
                               *common, "--reorder-blocks=ext-tsp"])
        run("profiled-execute", [profiled])
        profiled_code = inspect_notrack(disassembly("profiled-code", profiled), profiled,
                                        original_start=original_code["function_start"])
        report.update(passed=True, instrumented_code=inst_code, profiled_code=profiled_code,
                      profile_sha256=sha256(profile), target_nonzero_records=len(records))
    finally:
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: upstream/patched fixture, instrumentation, profile rewrite, prefix and moved tables.")


if __name__ == "__main__":
    main()
