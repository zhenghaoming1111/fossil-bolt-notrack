#!/usr/bin/env python3
"""Run frozen Fossil seed43/44 A/OFF/ON experiments, preserving the old runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SHA = 'b40d84fc14682861d85b48586fea699ce11b6d51917e792a65f0df4ff14195e6'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--apps', type=Path, required=True, help='Freshly prepared validate_bolt_mainline.sh output')
    parser.add_argument('--rounds', type=int, default=24)
    parser.add_argument('--target-seconds', type=float, default=3.0)
    parser.add_argument('--seeds', type=int, nargs='+', default=[43, 44])
    args = parser.parse_args()
    script = ROOT / 'scripts/fossil_lab.py'
    assert hashlib.sha256(script.read_bytes()).hexdigest() == SCRIPT_SHA
    tools, apps = args.tools.resolve(), args.apps.resolve()
    allowed = sorted(os.sched_getaffinity(0))
    assert len(allowed) >= 2 and args.rounds >= 24 and args.rounds % 6 == 0
    plan = {'rounds': args.rounds, 'target_seconds': args.target_seconds,
            'seeds': args.seeds, 'training_seed': 42, 'concurrency': 4,
            'server_cpu': allowed[0], 'client_cpu': allowed[1], 'order_seed': 20261118,
            'presets': ['dashboard', 'web'], 'two_warmups_per_variant_per_round': True,
            'legacy_labels': {'A-original': 'original', 'B-before-fix': 'same BOLT OFF', 'C-after-fix': 'same BOLT ON'},
            'actual_tool_manifest': json.loads((tools / 'manifest.json').read_text()),
            'frozen_script_sha256': SCRIPT_SHA}
    planfile = apps / 'formal-analysis-plan.json'
    if planfile.exists():
        raise RuntimeError('Preserving the original measurement plan; choose a new apps directory')
    planfile.write_text(json.dumps(plan, indent=2) + '\n')
    for preset in plan['presets']:
        lab = apps / ('fossil-' + preset)
        for seed in args.seeds:
            common = ['--lab', str(lab), '--preset', preset, '--cpu', str(allowed[0]),
                      '--client-cpu', str(allowed[1]), '--before', str(tools / 'before-fix/bin/llvm-bolt'),
                      '--after', str(tools / 'after-fix/bin/llvm-bolt'), '--concurrency', '4', '--seed', str(seed)]
            for action in ('check', 'benchmark'):
                command = ['python3', '-u', str(script), action, *common]
                if action == 'benchmark':
                    command += ['--rounds', str(args.rounds), '--order-seed', '20261118', '--target-seconds', str(args.target_seconds)]
                logpath = apps / (preset + '-seed' + str(seed) + '-' + action + '-console.txt')
                print('START:', preset, seed, action, flush=True)
                with logpath.open('w') as log:
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
                if action == 'benchmark':
                    latest = json.loads((lab / 'latest-benchmark.json').read_text())
                    (lab / ('benchmark-seed' + str(seed) + '.json')).write_text(json.dumps(latest, indent=2) + '\n')
                    summary = json.loads((Path(latest['directory']) / 'summary.json').read_text())
                    print(json.dumps({case: value['summary']['comparisons']['B-before-fix -> C-after-fix']
                                      for case, value in summary['cases'].items()}, indent=2), flush=True)
                print('PASS:', preset, seed, action, flush=True)


if __name__ == '__main__':
    main()
