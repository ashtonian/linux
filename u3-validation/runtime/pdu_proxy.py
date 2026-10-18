#!/usr/bin/env python3
"""Split H2CData PDUs between a loopback NVMe initiator and disposable target."""
import argparse
import json
import socket
import struct
import threading
import traceback

TABLE = []
for value in range(256):
    for _ in range(8):
        value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    TABLE.append(value)


def crc32c(data):
    crc = 0xFFFFFFFF
    for value in data:
        crc = TABLE[(crc ^ value) & 255] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


assert crc32c(b"123456789") == 0xE3069283
LOCK = threading.Lock()


def event(**fields):
    with LOCK:
        print(json.dumps(fields, sort_keys=True), flush=True)


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        block = sock.recv(count - len(data))
        if not block:
            raise EOFError()
        data.extend(block)
    return bytes(data)


def packet(sock):
    header = exact(sock, 8)
    size = struct.unpack_from("<I", header, 4)[0]
    if not 8 <= size <= 4 * 1024 * 1024 + 128:
        raise ValueError(f"PDU length {size} outside test bounds")
    return header + exact(sock, size - 8)


def validate(pdu):
    kind, flags, hlen, pdo, plen = struct.unpack_from("<BBBBI", pdu)
    assert len(pdu) == plen
    if kind in (0, 1):
        return
    if flags & 1:
        assert struct.unpack_from("<I", pdu, hlen)[0] == crc32c(pdu[:hlen])
    if flags & 2:
        assert pdo and struct.unpack_from("<I", pdu, plen - 4)[0] == crc32c(pdu[pdo:-4])


def split(pdu, pattern):
    kind, flags, hlen, pdo, _ = struct.unpack_from("<BBBBI", pdu)
    assert kind == 6 and hlen == 24
    cid, tag, offset, length = struct.unpack_from("<HHII", pdu, 8)
    data = pdu[pdo:pdo + length]
    assert len(data) == length and length % 4 == 0
    requested = [1000, 2000, 8192] if pattern == "spec" else [512, 4096]
    lengths = []
    remaining = length
    for size in requested:
        if size < remaining:
            lengths.append(size)
            remaining -= size
    lengths.append(remaining)
    done = 0
    result = []
    for index, size in enumerate(lengths):
        new_flags = flags if index + 1 == len(lengths) else flags & ~4
        new_pdo = hlen + (4 if flags & 1 else 0)
        plen = new_pdo + size + (4 if flags & 2 else 0)
        header = struct.pack("<BBBBIHHII4x", 6, new_flags, hlen, new_pdo,
                             plen, cid, tag, offset + done, size)
        body = header
        if flags & 1:
            body += struct.pack("<I", crc32c(header))
        chunk = data[done:done + size]
        body += chunk
        if flags & 2:
            body += struct.pack("<I", crc32c(chunk))
        validate(body)
        result.append(body)
        done += size
    assert done == length
    event(type="split", cid=cid, offset=offset, original_length=length,
          lengths=lengths, header_digest=bool(flags & 1), data_digest=bool(flags & 2))
    return result


def connection(client, target, number, pattern):
    upstream = socket.create_connection(("127.0.0.1", target), timeout=30)
    upstream.settimeout(None)
    client.settimeout(None)

    def relay(source, destination, direction):
        try:
            while True:
                pdu = packet(source)
                validate(pdu)
                kind = pdu[0]
                if kind == 4:
                    opcode = pdu[8]
                    length = struct.unpack_from("<I", pdu, 40)[0]
                    event(type="command", connection=number, opcode=opcode,
                          sgl_length=length, direction=direction)
                elif kind == 7:
                    event(type="read_data", connection=number,
                          length=struct.unpack_from("<I", pdu, 16)[0],
                          header_digest=bool(pdu[1] & 1), data_digest=bool(pdu[1] & 2))
                if kind == 6 and direction == "h2c":
                    for fragment in split(pdu, pattern):
                        destination.sendall(fragment)
                else:
                    destination.sendall(pdu)
        except (EOFError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as error:
            event(type="proxy_error", connection=number, direction=direction,
                  error=repr(error), traceback=traceback.format_exc())
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threads = [threading.Thread(target=relay, args=(client, upstream, "h2c")),
               threading.Thread(target=relay, args=(upstream, client, "c2h"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    client.close()
    upstream.close()
    event(type="connection_closed", connection=number)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=int, default=4421)
    parser.add_argument("--target", type=int, default=4420)
    parser.add_argument("--pattern", choices=("spec", "page"), default="spec")
    args = parser.parse_args()
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.listen))
    server.listen(16)
    event(type="ready", listen=args.listen, target=args.target, pattern=args.pattern)
    number = 0
    while True:
        client, _ = server.accept()
        number += 1
        threading.Thread(target=connection,
                         args=(client, args.target, number, args.pattern), daemon=True).start()


if __name__ == "__main__":
    main()
