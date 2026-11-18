#!/usr/bin/env python3
"""Create a fresh Fossil experiment from the distributed, hash-pinned inputs."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab', type=Path, required=True)
    parser.add_argument('--preset', choices=['web', 'dashboard'], required=True)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--client-cpu', type=int, required=True)
    parser.add_argument('--original', type=Path, help='Optional compatible prebuilt original; it is still probed and recorded.')
    args = parser.parse_args()
    lab = args.lab.resolve()
    if lab.exists():
        parser.error('Choose a NEW experiment directory; existing outputs are never replaced.')
    allowed = os.sched_getaffinity(0)
    if args.cpu == args.client_cpu or {args.cpu, args.client_cpu} - allowed:
        parser.error('Use two distinct CPUs from the current allowed CPU list.')
    for tool in (args.before, args.after):
        if not tool.is_file() or not (tool.parent.parent / 'lib/libbolt_rt_instr.a').is_file():
            parser.error('Missing llvm-bolt or its sibling ../lib/libbolt_rt_instr.a: ' + str(tool))
    manifest = json.loads((ROOT / 'datasets/manifest.json').read_text())
    for relative, expected in manifest['files_sha256'].items():
        path = ROOT / relative
        if sha256(path) != expected:
            raise RuntimeError('Distributed input hash mismatch: ' + relative)
    frozen_script = ROOT / 'scripts/fossil_lab.py'
    if sha256(frozen_script) != manifest['frozen_script_sha256']:
        raise RuntimeError('The historical benchmark script was modified.')
    if args.original and not args.original.is_file():
        parser.error('Prebuilt original does not exist: ' + str(args.original))
    for name in ('downloads', 'bin', 'data/traces', 'profiles', 'logs', 'scripts', 'results'):
        (lab / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'datasets/fossil-src-2.28.tar.gz', lab / 'downloads/fossil-src-2.28.tar.gz')
    shutil.copy2(frozen_script, lab / 'scripts/fossil_lab.py')
    if args.original:
        shutil.copy2(args.original, lab / 'bin/fossil.orig')
        (lab / 'bin/fossil.orig').chmod(0o755)
    # prepare builds only if the original is absent, and always probes the CFG.
    common = ['--lab', str(lab), '--preset', args.preset, '--cpu', str(args.cpu),
              '--client-cpu', str(args.client_cpu), '--before', str(args.before.resolve()),
              '--after', str(args.after.resolve())]
    subprocess.run([sys.executable, str(frozen_script), 'prepare', *common], check=True)
    snapshot = lab / 'data/snapshot.fossil'
    with gzip.open(ROOT / 'datasets/fossil-snapshot.fossil.gz', 'rb') as source, snapshot.open('wb') as target:
        shutil.copyfileobj(source, target)
    if sha256(snapshot) != manifest['snapshot_sha256']:
        raise RuntimeError('Uncompressed repository snapshot hash mismatch.')
    for source in (ROOT / 'datasets/traces').glob('*.json'):
        if (args.preset == 'dashboard') == source.name.startswith('dashboard-'):
            shutil.copy2(source, lab / 'data/traces' / source.name)
    family = 'dashboard' if args.preset == 'dashboard' else 'web'
    shutil.copy2(ROOT / 'evidence' / family / 'dataset.json', lab / 'dataset.json')
    subprocess.run([str(lab / 'bin/fossil.orig'), 'test-integrity', '--db-only', '-R', str(snapshot)], check=True)
    record = {'preset': args.preset, 'snapshot_sha256': sha256(snapshot),
              'original_sha256': sha256(lab / 'bin/fossil.orig'),
              'prebuilt_original': str(args.original.resolve()) if args.original else None,
              'tool_before_sha256': sha256(args.before), 'tool_after_sha256': sha256(args.after),
              'note': 'Input initialization only. Train, rewrite and check are required before timing.'}
    (lab / 'initialization.json').write_text(json.dumps(record, indent=2) + '\n')
    subprocess.run([sys.executable, str(ROOT / 'scripts/collect_environment.py'),
                    '--output', str(lab / 'host-environment.json')], check=True)
    print('READY: fixed repository and traces installed without contacting the live upstream.')
    print(lab)


if __name__ == '__main__':
    main()
