#!/usr/bin/env python3
"""Save one actual BOLT executable plus explicit OFF/ON launchers."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(build, output, config):
    output.mkdir(parents=True, exist_ok=False)
    common = output / 'common'
    for directory in ('bin', 'lib'):
        (common / directory).mkdir(parents=True)
    for name in ('llvm-bolt', 'llvm-objdump', 'llvm-mc', 'FileCheck'):
        shutil.copy2(build / 'bin' / name, common / 'bin' / name)
    shutil.copy2(build / 'lib/libbolt_rt_instr.a', common / 'lib/libbolt_rt_instr.a')
    for name, value in (('before-fix', 'false'), ('after-fix', 'true')):
        wrapper = output / name / 'bin/llvm-bolt'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/usr/bin/env bash\nset -euo pipefail\nbase=$(cd -- "$(dirname -- "$0")/../.." && pwd)\nexec "$base/common/bin/llvm-bolt" --x86-notrack-jump-tables=' + value + ' "$@"\n')
        wrapper.chmod(0o755)
        (wrapper.parent.parent / 'lib').mkdir()
        shutil.copy2(common / 'lib/libbolt_rt_instr.a', wrapper.parent.parent / 'lib/libbolt_rt_instr.a')
    metadata = {'upstream_commit': config['upstream_commit'], 'option': config['option_name'],
                'default': config['option_default'], 'both_modes_use_same_executable': True,
                'actual_executable_sha256': digest(common / 'bin/llvm-bolt'),
                'patch_sha256': config['patch_sha256'],
                'legacy_labels': {'before-fix': 'same executable, explicit OFF',
                                  'after-fix': 'same executable, explicit ON'},
                'files': {str(p.relative_to(output)): digest(p) for p in sorted(output.rglob('*')) if p.is_file()}}
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'configs/llvm-mainline-options.json')
    args = parser.parse_args()
    install(args.build.resolve(), args.output.resolve(), json.loads(args.config.read_text()))
