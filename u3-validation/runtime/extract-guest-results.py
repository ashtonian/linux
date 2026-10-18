#!/usr/bin/env python3
"""Extract the bounded text evidence exported by the disposable guest."""
import argparse
import base64
import io
import json
from pathlib import Path
import re
import tarfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('console', type=Path)
parser.add_argument('destination', type=Path)
args = parser.parse_args()
console = args.console.read_text(errors='replace')
encoded = console.split('U3_ARTIFACT_BEGIN\n', 1)[1].split('U3_ARTIFACT_END', 1)[0]
encoded = ''.join(line.strip() for line in encoded.splitlines()
                  if re.fullmatch(r'[A-Za-z0-9+/=]+', line.strip()))
raw = base64.b64decode(encoded, validate=True)
args.destination.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
    for member in archive.getmembers():
        if member.isdir():
            continue
        path = Path(member.name)
        if not member.isfile() or path.is_absolute() or '..' in path.parts:
            raise ValueError(f'Unexpected guest artifact: {member.name}')
        if member.size > 10 * 1024 * 1024:
            raise ValueError(f'Guest artifact too large: {member.name}')
        dest = args.destination / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.extractfile(member).read())
print('Extracted guest text evidence to', args.destination)
report = json.loads((args.destination / 'results.json').read_text())
assert re.search(r'^U3_GUEST_EXIT=0$', console, re.MULTILINE), 'guest failed'
assert report[-1]['test'] == 'complete' and report[-1]['status'] == 'PASS'
assert not any(row.get('status') == 'FAIL' for row in report)
print('Guest suite PASS:', len(report), 'result records')
