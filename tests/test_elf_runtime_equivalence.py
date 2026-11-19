"""Guard the observed metadata false positive without hiding executable changes."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from elf_runtime_equivalence import compare


def fixture(note_text=b"BOLT revision: fixture\0command line: --option=false\0"):
    names = b"\0.text\0.data\0.shstrtab\0.note.bolt_info\0"
    note_start = 0x300
    shoff = (note_start + len(note_text) + 15) & ~15
    sections = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                (1, 1, 6, 0x400100, 0x100, 16, 0, 0, 16, 0),
                (7, 1, 3, 0x400180, 0x180, 16, 0, 0, 8, 0),
                (13, 3, 0, 0, 0x240, len(names), 0, 0, 1, 0),
                (23, 7, 0, 0, note_start, len(note_text), 0, 0, 4, 0)]
    data = bytearray(shoff + 64 * len(sections))
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<16sHHIQQQIHHHHHH", data, 0, ident, 2, 62, 1, 0x400100, 64, shoff,
                     0, 64, 56, 1, 64, len(sections), 3)
    struct.pack_into("<II6Q", data, 64, 1, 7, 0, 0x400000, 0x400000, 0x200, 0x200, 0x1000)
    data[0x100:0x110] = b"\x90" * 15 + b"\xc3"
    data[0x180:0x190] = bytes(range(16))
    data[0x240:0x240 + len(names)] = names
    data[note_start:note_start + len(note_text)] = note_text
    for index, section in enumerate(sections):
        struct.pack_into("<IIQQQQIIQQ", data, shoff + index * 64, *section)
    return data


class RuntimeEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.off = Path(self.directory.name) / "off"
        self.on = Path(self.directory.name) / "on"
        self.left = fixture()
        self.right = fixture(b"BOLT revision: fixture\0command line: --option=true --output=longer-name\0")

    def check(self):
        self.off.write_bytes(self.left)
        self.on.write_bytes(self.right)
        return compare(self.off, self.on)

    def test_identical_program(self):
        self.right = self.left[:]
        self.assertTrue(self.check()["raw_load_segments_identical"])

    def test_only_note_and_section_table_offset_change(self):
        result = self.check()
        self.assertTrue(result["runtime_image_equivalent"])
        self.assertFalse(result["raw_load_segments_identical"])
        self.assertTrue(result["actual_loaded_differing_offsets"])
        self.assertEqual(result["changed_sections"], [".note.bolt_info"])

    def test_changed_instruction_rejected(self):
        self.right[0x100] ^= 1
        with self.assertRaisesRegex(ValueError, "Executable/data"):
            self.check()

    def test_changed_data_rejected(self):
        self.right[0x180] ^= 1
        with self.assertRaisesRegex(ValueError, "Executable/data"):
            self.check()

    def test_changed_entry_rejected(self):
        self.right[24] ^= 1
        with self.assertRaisesRegex(ValueError, "headers differ"):
            self.check()

    def test_changed_program_permissions_rejected(self):
        self.right[68] ^= 1
        with self.assertRaisesRegex(ValueError, "Program headers"):
            self.check()

    def test_changed_nonallocated_section_rejected(self):
        self.right[0x241] ^= 1
        with self.assertRaises(ValueError):
            self.check()

    def test_section_table_inside_program_segment_rejected(self):
        struct.pack_into("<Q", self.right, 64 + 32, len(self.right))
        struct.pack_into("<Q", self.right, 64 + 40, len(self.right))
        with self.assertRaisesRegex(ValueError, "Section table overlaps"):
            self.check()

    def test_allocated_bolt_note_rejected(self):
        shoff = struct.unpack_from("<Q", self.right, 40)[0]
        struct.pack_into("<Q", self.right, shoff + 4 * 64 + 8, 2)
        with self.assertRaisesRegex(ValueError, "nonallocated"):
            self.check()

    def test_truncated_table_rejected(self):
        self.right = self.right[:-1]
        with self.assertRaisesRegex(ValueError, "Truncated"):
            self.check()


if __name__ == "__main__":
    unittest.main()
