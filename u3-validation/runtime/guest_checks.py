#!/usr/bin/env python3
"""Focused NVMe/TCP tests restricted to the disposable U3 QEMU guest."""
import errno
import hashlib
import json
import os
import re
from pathlib import Path
import struct
import subprocess
import time

ROOT = Path('/sys/kernel/config/nvmet')
RESULTS = Path('/results')
RESULTS.mkdir(exist_ok=True)
REPORT = []
DEVICES = {}


def record(name, **details):
    row = {'test': name, **details}
    REPORT.append(row)
    (RESULTS / 'results.json').write_text(json.dumps(REPORT, indent=2) + '\n')
    print('U3_RESULT ' + json.dumps(row, sort_keys=True), flush=True)


def run(*args, timeout=90, binary=False):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args!r}: {result.returncode}: {result.stderr.decode(errors="replace")}')
    return result.stdout if binary else result.stdout.decode()


def write(path, value):
    Path(path).write_text(str(value))


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError('guest state transition timed out')


def tcp_controllers():
    controllers = set()
    for path in Path('/sys/class/nvme').glob('nvme*'):
        try:
            if (path / 'transport').read_text().strip() == 'tcp':
                controllers.add(path.name)
        except FileNotFoundError:
            pass
    return controllers


def connect(nqn, service, digests):
    before = tcp_controllers()
    before_namespaces = {p.name for p in Path('/sys/block').glob('nvme*')
                         if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)}
    args = ['nvme', 'connect', '-t', 'tcp', '-a', '127.0.0.1', '-s', str(service),
            '-n', nqn, '--nr-io-queues=1', '--queue-size=16',
            '--keep-alive-tmo=0', '--ctrl-loss-tmo=5', '--reconnect-delay=1']
    if digests:
        args += ['--hdr-digest', '--data-digest']
    run(*args)
    name = next(iter(wait_for(lambda: tcp_controllers() - before)))
    namespace = next(iter(wait_for(lambda: {p.name for p in Path('/sys/block').glob('nvme*')
                        if re.fullmatch(r'nvme[0-9]+n[0-9]+', p.name)} - before_namespaces)))
    wait_for(lambda: Path('/dev/' + namespace).exists())
    DEVICES['/dev/' + name] = '/dev/' + namespace
    return '/dev/' + name


def disconnect(controller):
    run('nvme', 'disconnect', '-d', controller)
    wait_for(lambda: Path(controller).name not in tcp_controllers())
    wait_for(lambda: not Path(DEVICES[controller]).exists())
    DEVICES.pop(controller)


def mdts(controller):
    data = run('nvme', 'id-ctrl', controller, '-b', binary=True)
    assert len(data) == 4096
    return data[77]


def io_roundtrip(controller, length, label):
    device = DEVICES[controller]
    namespace = run('nvme', 'id-ns', device, '-b', binary=True)
    fmt = namespace[26] & 15
    shift = namespace[128 + fmt * 4 + 2]
    block = 1 << shift
    assert length % block == 0
    payload = bytes((i * 37 + i // 257) & 255 for i in range(length))
    source = RESULTS / 'write.bin'
    dest = RESULTS / 'read.bin'
    source.write_bytes(payload)
    run('nvme', 'write', device, '--start-block=0', '--block-count=' + str(length // block - 1),
        '--data-size=' + str(length), '--data=' + str(source))
    run('nvme', 'flush', device)
    dest.write_bytes(b'')
    run('nvme', 'read', device, '--start-block=0', '--block-count=' + str(length // block - 1),
        '--data-size=' + str(length), '--data=' + str(dest))
    actual = dest.read_bytes()
    assert actual == payload, (label, len(actual), length)
    record(label, status='PASS', bytes=length, lba_size=block,
           sha256=hashlib.sha256(actual).hexdigest())


def block_roundtrip(controller, label):
    length = 2 * 1024 * 1024
    payload = bytes((i * 19 + i // 4093) & 255 for i in range(length))
    source, dest = RESULTS / 'write.bin', RESULTS / 'read.bin'
    source.write_bytes(payload)
    run('dd', 'if=' + str(source), 'of=' + DEVICES[controller],
        'bs=2M', 'count=1', 'oflag=direct', 'status=none')
    run('dd', 'if=' + DEVICES[controller], 'of=' + str(dest),
        'bs=2M', 'count=1', 'iflag=direct', 'status=none')
    assert dest.read_bytes() == payload
    record(label, status='PASS', application_io_bytes=length,
           sha256=hashlib.sha256(payload).hexdigest())


def subsystem(nqn, backend=None, passthrough=False):
    sub = ROOT / 'subsystems' / nqn
    sub.mkdir()
    write(sub / 'attr_allow_any_host', 1)
    if passthrough:
        write(sub / 'passthru/device_path', backend)
        write(sub / 'passthru/enable', 1)
    else:
        namespace = sub / 'namespaces/1'
        namespace.mkdir()
        write(namespace / 'device_path', backend)
        write(namespace / 'buffered_io', 1)
        write(namespace / 'enable', 1)
    return sub


def proxy(pattern, port, label=None):
    log = RESULTS / f'proxy-{label or pattern}.jsonl'
    handle = log.open('w')
    proc = subprocess.Popen(['python3', '/u3/pdu_proxy.py', '--listen', str(port),
                             '--pattern', pattern], stdout=handle, stderr=handle)
    wait_for(lambda: log.exists() and '"ready"' in log.read_text())
    return proc, handle, log


def unload(module):
    deadline = time.monotonic() + 15
    while True:
        result = subprocess.run(['modprobe', '-r', module], capture_output=True)
        if result.returncode == 0:
            return
        if b'is in use' not in result.stderr or time.monotonic() >= deadline:
            raise RuntimeError(result.stderr.decode())
        time.sleep(0.2)


def main():
    assert 'u3_disposable=1' in Path('/proc/cmdline').read_text()
    wait_for(lambda: Path('/dev/nvme0n1').exists())
    assert Path('/sys/class/nvme/nvme0/serial').read_text().strip() == 'U3TEST'
    record('environment', kernel=run('uname', '-r').strip(),
           backend_serial='U3TEST', network='loopback-only',
           tools=run('nvme', 'version').strip())
    backing = Path('/tmp/u3-namespace.img')
    with backing.open('wb') as stream:
        stream.truncate(64 * 1024 * 1024)
    nqn = 'nqn.2026-10.test:u3-file'
    sub = subsystem(nqn, str(backing))
    port = ROOT / 'ports/1'
    port.mkdir()
    for attr, value in [('addr_trtype', 'tcp'), ('addr_adrfam', 'ipv4'),
                        ('addr_traddr', '127.0.0.1'), ('addr_trsvcid', '4420'),
                        ('param_inline_data_size', 4096)]:
        write(port / attr, value)
    attr = port / 'param_mdts'
    link = port / 'subsystems' / nqn
    write(attr, 8)
    for invalid in ['-1', '256', '2147483648', 'bad']:
        try:
            write(attr, invalid)
        except OSError as error:
            assert error.errno == errno.EINVAL, error
        else:
            raise AssertionError(f'accepted invalid MDTS {invalid}')
        assert attr.read_text().strip() == '8'
    record('configfs_invalid_preserves_value', status='PASS', previous=8)
    for requested in [0, 1, 6, 8, 10, 13, 255]:
        write(attr, requested)
        link.symlink_to(sub)
        controller = connect(nqn, 4420, False)
        try:
            actual = mdts(controller)
            expected = min(requested or 8, 8)
            assert actual == expected, (requested, actual, expected)
            name = Path(DEVICES[controller]).name
            queue_limit = Path('/sys/block/' + name + '/queue/max_hw_sectors_kb').read_text().strip()
            record('identify_mdts', status='PASS', requested=requested,
                   advertised=actual, max_hw_sectors_kb=int(queue_limit))
            try:
                write(attr, 6 if requested != 6 else 8)
            except OSError as error:
                assert error.errno == errno.EACCES, error
            else:
                raise AssertionError('active port accepted an MDTS change')
            assert attr.read_text().strip() == str(requested)
        finally:
            disconnect(controller)
            link.unlink()
    record('configfs_active_port_unchanged', status='PASS')
    write(attr, 8)
    link.symlink_to(sub)
    for index, pattern in enumerate(['spec', 'page']):
        proc, handle, log = proxy(pattern, 4421 + index)
        try:
            for digests in [False, True]:
                controller = connect(nqn, 4421 + index, digests)
                try:
                    for length in [12288, 1048576]:
                        io_roundtrip(controller, length, f'{pattern}_digests_{digests}_{length}')
                    block_roundtrip(controller, f'host_split_{pattern}_digests_{digests}')
                finally:
                    disconnect(controller)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            assert not any(e['type'] == 'proxy_error' for e in events), events
            fragments = [e for e in events if e['type'] == 'split']
            assert len(fragments) >= 4
            assert any(e['data_digest'] for e in fragments)
            assert any(not e['data_digest'] for e in fragments)
            expected = [1000, 2000, 8192, 1096] if pattern == 'spec' else [512, 4096, 7680]
            assert any(e['lengths'] == expected for e in fragments)
            writes = [e['sgl_length'] for e in events if e['type'] == 'command' and e['opcode'] == 1]
            assert max(writes) == 1048576
            assert all(length <= 1048576 for length in writes)
            record('wire_evidence_' + pattern, status='PASS', split_pdus=len(fragments),
                   expected_fragments=expected, largest_write=max(writes))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            handle.close()
    link.unlink()
    write(attr, 6)
    link.symlink_to(sub)
    proc, handle, log = proxy('page', 4423, label='lower-mdts')
    try:
        controller = connect(nqn, 4423, True)
        try:
            assert mdts(controller) == 6
            block_roundtrip(controller, 'host_split_configured_256k')
        finally:
            disconnect(controller)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        assert not any(e['type'] == 'proxy_error' for e in events)
        writes = [e['sgl_length'] for e in events if e['type'] == 'command' and e['opcode'] == 1]
        assert writes and max(writes) <= 262144 and sum(writes) == 2097152
        record('wire_configured_256k', status='PASS', commands=len(writes),
               largest_write=max(writes), total_bytes=sum(writes))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        handle.close()
    link.unlink()
    # QEMU's backend MDTS is deliberately 128 KiB, below TCP's 1 MiB limit.
    physical_mdts = mdts('/dev/nvme0')
    assert physical_mdts == 5, physical_mdts
    passthru_nqn = 'nqn.2026-10.test:u3-passthru'
    passthru = subsystem(passthru_nqn, '/dev/nvme0', passthrough=True)
    passthru_link = port / 'subsystems' / passthru_nqn
    for requested, expected in [(0, 5), (4, 4), (8, 5), (13, 5)]:
        write(attr, requested)
        passthru_link.symlink_to(passthru)
        controller = connect(passthru_nqn, 4420, True)
        try:
            actual = mdts(controller)
            assert actual == expected, (requested, actual, expected)
            record('passthrough_mdts', status='PASS', requested=requested,
                   backend=physical_mdts, advertised=actual)
            io_roundtrip(controller, 32768, f'passthrough_io_mdts_{requested}')
        finally:
            disconnect(controller)
            passthru_link.unlink()
    write(passthru / 'passthru/enable', 0)
    write(sub / 'namespaces/1/enable', 0)
    unload('nvme_tcp')
    unload('nvmet_tcp')
    record('module_teardown', status='PASS')
    record('complete', status='PASS', checks=len(REPORT))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        record('failure', status='FAIL', error=repr(error), traceback=traceback.format_exc())
        raise
