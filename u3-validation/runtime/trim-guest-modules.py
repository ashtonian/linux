#!/usr/bin/env python3
"""Keep only the disposable guest's NVMe module dependency closure."""
from pathlib import Path
import shutil
import subprocess
import sys

root = Path('/work/guest-root')
release = sys.argv[1]
modules = root / 'lib/modules' / release
keep = set()
for name in ['nvme', 'nvmet', 'nvmet_tcp', 'nvme_tcp']:
    output = subprocess.check_output(['modprobe', '-d', str(root), '-S', release,
                                      '--show-depends', name], text=True)
    keep.update(Path(line.split()[1]) for line in output.splitlines()
                if line.startswith('insmod '))
assert keep and all(path.is_file() and path.is_relative_to(modules) for path in keep)
for other in modules.parent.iterdir():
    if other.is_dir() and other != modules:
        shutil.rmtree(other)
for path in modules.rglob('*.ko*'):
    if path not in keep:
        path.unlink()
subprocess.run(['depmod', '-b', str(root), release], check=True)
print('Guest module closure:', len(keep), 'files')
