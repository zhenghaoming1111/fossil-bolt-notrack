#!/usr/bin/env python3
"""Record the environment visible to a Linux benchmark; no privileged reads."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def command(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return {"argv": argv, "returncode": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"argv": argv, "error": str(error)}


def collect():
    cpu_paths = sorted(Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_governor'))
    return {
        'collected_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'measurement_note': 'Snapshot at collection time; frequency, load and memory are not historical measurements.',
        'platform': platform.platform(),
        'is_wsl': 'microsoft' in platform.release().lower(),
        'allowed_cpus': sorted(os.sched_getaffinity(0)),
        'os_release': Path('/etc/os-release').read_text(),
        'meminfo': Path('/proc/meminfo').read_text(),
        'cpu_governors': {str(p): p.read_text().strip() for p in cpu_paths},
        'commands': {label: command(argv) for label, argv in {
            'uname': ['uname', '-a'], 'lscpu': ['lscpu'],
            'lscpu_topology': ['lscpu', '-e=CPU,CORE,SOCKET,ONLINE'],
            'memory': ['free', '-b'], 'disk': ['df', '-h', '.'],
            'gcc': ['gcc', '--version'], 'clang': ['clang-18', '--version'],
            'python': ['python3', '--version'], 'ldd': ['ldd', '--version'],
            'packages': ['dpkg-query', '-W', 'libc6', 'libssl3t64', 'zlib1g'],
            'docker': ['docker', '--version'],
        }.items()},
        'tools': {name: shutil.which(name) for name in ['gcc', 'python3', 'perf', 'docker']},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collect(), ensure_ascii=False, indent=2) + '\n')
    print(args.output)
