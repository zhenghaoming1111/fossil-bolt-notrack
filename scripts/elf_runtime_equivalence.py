#!/usr/bin/env python3
"""Compare BOLT ELF outputs, permitting only audited non-runtime metadata changes.

This is deliberately stricter than comparing disassembly. Instructions, data,
program headers, entry point and all other sections must remain identical.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def require(condition, message):
    if not condition:
        raise ValueError(message)


def overlaps(start, size, other_start, other_size):
    return size > 0 and other_size > 0 and start < other_start + other_size and other_start < start + size


def inspect(path):
    data = Path(path).read_bytes()
    require(len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01", "Require little-endian ELF64")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    _, kind, machine, version, entry, phoff, shoff, _, ehsize, phsize, phnum, shsize, shnum, shstr = header
    require(machine == 62 and kind in (2, 3) and version == 1 and ehsize == 64, "Require x86-64 executable ELF")
    require(phsize == 56 and phnum > 0 and shsize == 64 and 0 < shnum < 0xff00,
            "Unsupported or missing ELF tables (extended numbering is not supported)")

    def region(start, size):
        require(0 <= start <= len(data) and 0 <= size <= len(data) - start, "Truncated ELF range")
        return data[start:start + size]

    region(phoff, phsize * phnum)
    region(shoff, shsize * shnum)
    require(phoff >= ehsize and shoff >= ehsize and 0 < shstr < shnum, "Invalid ELF table location/index")
    programs = [struct.unpack_from("<II6Q", data, phoff + i * phsize) for i in range(phnum)]
    loads = [p for p in programs if p[0] == 1]
    require(bool(loads), "Missing PT_LOAD segments")
    for p in programs:
        region(p[2], p[5])
        if p[0] == 1:
            require(p[5] <= p[6], "PT_LOAD file size exceeds memory size")
    # Also reject a section table exposed through a non-LOAD program header.
    require(not any(overlaps(shoff, shsize * shnum, p[2], p[5]) for p in programs if p[0]),
            "Section table overlaps a program segment; e_shoff cannot be ignored")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shsize) for i in range(shnum)]
    strings = sections[shstr]
    require(strings[1] == 3, "Invalid section-name string table")
    names = region(strings[4], strings[5])
    named = {}
    for section in sections:
        require(section[0] < len(names), "Invalid section name offset")
        end = names.find(b"\0", section[0])
        require(end >= 0, "Unterminated section name")
        name = names[section[0]:end].decode("utf-8")
        require(name not in named, "Duplicate section name")
        content = b"" if section[1] == 8 else region(section[4], section[5])
        named[name] = (section, content)
    require(".note.bolt_info" in named, "Missing BOLT command note")
    note, content = named[".note.bolt_info"]
    require(note[1] == 7 and not note[2] & 2, "BOLT note must be nonallocated SHT_NOTE")
    require(not any(overlaps(note[4], note[5], p[2], p[5]) for p in programs if p[0]),
            "BOLT note overlaps a program segment")
    require(not overlaps(note[4], note[5], shoff, shsize * shnum), "BOLT note overlaps section table")
    require(b"BOLT revision:" in content and b"command line:" in content, "Unrecognized BOLT command note")
    normalized = bytearray(data)
    normalized[40:48] = b"\0" * 8
    image = {"entry": entry, "segments": []}
    raw_image = {"entry": entry, "segments": []}
    for p in loads:
        _, flags, start, vaddr, paddr, filesz, memsz, align = p
        fields = {"vaddr": vaddr, "paddr": paddr, "offset": start, "filesz": filesz,
                  "memsz": memsz, "flags": flags, "align": align}
        image["segments"].append({**fields, "sha256": hashlib.sha256(normalized[start:start + filesz]).hexdigest()})
        raw_image["segments"].append({**fields, "sha256": hashlib.sha256(data[start:start + filesz]).hexdigest()})
    return {"data": data, "programs": programs, "loads": loads, "sections": named, "shoff": shoff,
            "runtime_image": image, "raw_image": raw_image, "header": normalized[:64]}


def compare(off, on):
    a, b = inspect(off), inspect(on)
    require(a["header"] == b["header"], "ELF headers differ beyond e_shoff")
    require(a["programs"] == b["programs"], "Program headers differ")
    require(a["runtime_image"] == b["runtime_image"], "Executable/data bytes differ")
    require(list(a["sections"]) == list(b["sections"]), "Section names/order differ")
    changed = []
    for name in a["sections"]:
        if a["sections"][name] == b["sections"][name]:
            continue
        require(name == ".note.bolt_info", "Unexpected section difference: " + name)
        left, right = list(a["sections"][name][0]), list(b["sections"][name][0])
        left[5] = right[5] = 0  # Only the nonallocated command-note size may vary.
        require(left == right, "Unexpected BOLT note header difference")
        changed.append(name)
    require(a["shoff"] == b["shoff"] or bool(changed), "Unexplained section table relocation")
    differences = []
    for p in a["loads"]:
        start, end = p[2], p[2] + p[5]
        # Most segments are identical; avoid walking large identical byte arrays.
        if a["data"][start:end] != b["data"][start:end]:
            differences.extend(i for i in range(start, end) if a["data"][i] != b["data"][i])
    require(all(40 <= i < 48 for i in differences), "Unexpected loaded byte difference")
    return {"status": "PASS", "runtime_image_equivalent": True,
            "raw_load_segments_identical": not differences,
            "comparison_policy": "x86-64-elf-bolt-note-v1",
            "allowed_metadata": "Nonallocated .note.bolt_info content/size; e_shoff only when that note changes. Both note and section table must be outside every program segment.",
            "actual_loaded_differing_offsets": sorted(set(differences)),
            "changed_sections": changed,
            "section_table_offsets": {"off": a["shoff"], "on": b["shoff"]},
            "raw_images": {"off": a["raw_image"], "on": b["raw_image"]},
            "normalized_images": {"off": a["runtime_image"], "on": b["runtime_image"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("off", type=Path)
    parser.add_argument("on", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = compare(args.off, args.on)
    except (ValueError, UnicodeError, OSError, struct.error) as error:
        parser.exit(1, "FAIL: " + str(error) + "\n")
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
