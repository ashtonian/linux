#!/usr/bin/env python3
"""Record completed local packages and their exact build inputs."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/work')
OUT = ROOT / 'qualification'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


manifest = {
    'candidate': '9d9d9d7bd620d4c8ef6358e163ca78949e11c2f2',
    'base': 'e43b65255bec4614c14447eb6152711e3e6e8124',
    'builder': 'Ubuntu 22.04/aarch64, cross-compiling x86_64',
    'archive_compression': 'KDEB_COMPRESS=gzip; dpkg-deb-local sets build level 1',
    'compiler': subprocess.check_output(['x86_64-linux-gnu-gcc', '--version'], text=True),
    'packages': [], 'variants': {},
    'limitations': 'Local cross-build, not hosted CI or a TrueNAS appliance image. '
                   'See perf-build-options.txt for optional perf capabilities.',
}
for variant in ['production', 'debug']:
    source = ROOT / ('package-' + variant)
    exit_file = OUT / (variant + '-package.exit')
    manifest['variants'][variant] = {
        'exit_status': int(exit_file.read_text()),
        'config_sha256': sha256(source / '.config'),
        'kernel_sha256': sha256(source / 'arch/x86/boot/bzImage'),
        'source_sha256': {str(path.relative_to(source)): sha256(path)
                          for path in [source / 'drivers/nvme/target' / filename
                          for filename in ['admin-cmd.c', 'configfs.c', 'core.c',
                                           'nvmet.h', 'passthru.c', 'tcp.c', 'zns.c']]},
    }
for path in sorted(ROOT.glob('*.deb')):
    info = subprocess.check_output(['dpkg-deb', '-f', str(path),
                                    'Package', 'Version', 'Architecture'], text=True)
    manifest['packages'].append({'file': path.name, 'bytes': path.stat().st_size,
                                  'sha256': sha256(path), 'control': info.strip()})
(OUT / 'packages.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps({'variants': {k: v['exit_status'] for k, v in manifest['variants'].items()},
                  'package_count': len(manifest['packages'])}, indent=2))
