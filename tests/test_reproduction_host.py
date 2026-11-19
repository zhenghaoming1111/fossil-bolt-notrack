"""Exercise preflight failure boundaries without a Docker service."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reproduction_host as host


class HostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.results = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(host.os, "getuid", return_value=1000, create=True).start()
        patch.object(host.os, "getgid", return_value=1000, create=True).start()
        patch.object(host, "check_source_files").start()
        self.args = argparse.Namespace(image="test-image")
        self.config = {"release": "test", "llvm_commit": "fixed"}
        self.snapshot = {"busy_processes": [], "cpu_pmu_sysfs_present": False}
        self.manifest = hashlib.sha256((host.ROOT / "SOURCE_SHA256SUMS").read_bytes()).hexdigest()

    def outputs(self, manifest=None):
        self.args.image_manifest = self.results / "identity.json"
        self.args.image_manifest.write_text(json.dumps({"release": "test", "source_manifest_sha256": self.manifest,
                                                        "rootfs_diff_ids": ["sha256:test"]}))
        return [json.dumps({"ServerVersion": "test"}),
                json.dumps([{"Os": "linux", "Architecture": "amd64", "RootFS": {"Layers": ["sha256:test"]}, "Config": {
                    "Labels": {"org.opencontainers.image.revision": "fixed"}}}]),
                (manifest or self.manifest) + "  SOURCE_SHA256SUMS\n",
                json.dumps({"allowed_cpus": [4, 5], "server_cpu": 4, "client_cpu": 5})]

    def test_no_pmu_is_a_warning(self):
        with patch.object(host, "host_check", return_value=self.snapshot), patch.object(host, "capture", side_effect=self.outputs()):
            result = host.preflight(self.args, self.config, self.results, ["docker"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(any("PMU" in message for message in result["warnings"]))

    def test_stale_image_rejected_before_binary_probe(self):
        with patch.object(host, "host_check", return_value=self.snapshot), patch.object(host, "capture", side_effect=self.outputs("0" * 64)) as called:
            result = host.preflight(self.args, self.config, self.results, ["docker"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("checkout differ", result["error"])
        self.assertEqual(called.call_count, 3)

    def test_daemon_failure_recorded(self):
        with patch.object(host, "host_check", return_value=self.snapshot), patch.object(host, "capture", side_effect=OSError("not installed")):
            result = host.preflight(self.args, self.config, self.results, ["docker"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("daemon is unavailable", result["error"])
        self.assertTrue((self.results / result["record_file"]).is_file())

    def test_active_update_blocks_timing_before_creating_stage(self):
        with patch.object(host, "busy_processes", return_value=[{"pid": 1, "name": "apt"}]):
            with self.assertRaisesRegex(RuntimeError, "wait for them"):
                host.execute_stage("negative", self.args, self.results, ["docker"], "snapshot.json")
        self.assertEqual(list(self.results.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
