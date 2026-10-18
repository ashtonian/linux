#!/usr/bin/env python3
"""Check extracted receive vectors, per-PDU digests and cleanup with kernel API stubs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from check_mdts import function


PRELUDE = r"""
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef uint32_t u32;
#define PAGE_SIZE 4096
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
#define ITER_DEST 0
#define cpu_to_le32(value) (value)
#define NVME_TCP_DIGEST_LENGTH 4
#define NVMET_TCP_RECV_DDGST 3
struct scatterlist { unsigned int length, offset; bool last; unsigned char *data; };
struct bio_vec { void *page; unsigned int length, offset; };
struct iov_iter { struct bio_vec *bvec; unsigned int nr_segs; size_t count; };
struct nvmet_tcp_queue { int offset, left, rcv_state; };
struct nvmet_tcp_cmd {
    struct nvmet_tcp_queue *queue;
    struct bio_vec *iov;
    struct { struct scatterlist *sg; unsigned int sg_cnt, transfer_len; } req;
    u32 pdu_len, rbytes_done;
    u32 exp_ddgst;
    int sg_idx;
    struct { struct iov_iter msg_iter; } recv_msg;
};
static unsigned int checks, failures, vectors_built, vector_frees, sg_frees;
#define CHECK(condition) do { checks++; if (!(condition)) { \
    fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition); failures++; \
} } while (0)
static struct scatterlist *sg_next(struct scatterlist *sg)
{ return sg->last ? NULL : sg + 1; }
static void *sg_page(struct scatterlist *sg) { return sg; }
static unsigned char *sg_virt(struct scatterlist *sg) { return sg->data + sg->offset; }
static u32 crc32c(u32 crc, const unsigned char *data, size_t length)
{
    while (length--) {
        crc ^= *data++;
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ ((crc & 1) ? 0x82f63b78 : 0);
    }
    return crc;
}
static void bvec_set_page(struct bio_vec *bvec, void *page, unsigned int length,
                         unsigned int offset)
{
    *bvec = (struct bio_vec){ .page = page, .length = length, .offset = offset };
    vectors_built++;
}
static void iov_iter_bvec(struct iov_iter *iter, int direction,
                         struct bio_vec *bvec, unsigned int nr_segs, size_t count)
{
    (void)direction;
    *iter = (struct iov_iter){ .bvec = bvec, .nr_segs = nr_segs, .count = count };
}
static void kfree(void *p) { if (p) vector_frees++; free(p); }
static void sgl_free(void *p) { if (p) sg_frees++; free(p); }
"""


TESTS = r"""
static void check_span(unsigned int total, unsigned int offset, unsigned int length)
{
    unsigned int count = DIV_ROUND_UP(total, PAGE_SIZE);
    struct scatterlist *sg = calloc(count, sizeof(*sg));
    struct bio_vec *iov = calloc(count, sizeof(*iov));
    if (!sg || !iov) abort();
    for (unsigned int i = 0; i < count; i++) {
        sg[i].length = min_t(u32, PAGE_SIZE, total - i * PAGE_SIZE);
        sg[i].last = i + 1 == count;
    }
    struct nvmet_tcp_cmd cmd = { .iov = iov, .req = { .sg = sg, .sg_cnt = count },
                                .rbytes_done = offset, .pdu_len = length };
    vectors_built = 0;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == 0);
    CHECK(cmd.recv_msg.msg_iter.bvec == iov);
    CHECK(cmd.recv_msg.msg_iter.count == length);
    CHECK(cmd.recv_msg.msg_iter.nr_segs == vectors_built);
    size_t bytes = 0;
    for (unsigned int i = 0; i < cmd.recv_msg.msg_iter.nr_segs; i++)
        bytes += iov[i].length;
    CHECK(bytes == length);
    CHECK(iov[0].offset == offset % PAGE_SIZE);
    CHECK(cmd.sg_idx == (int)(offset / PAGE_SIZE));
    nvmet_tcp_free_cmd_buffers(&cmd);
    CHECK(!cmd.req.sg && !cmd.iov);
    unsigned int old_sg_frees = sg_frees, old_vector_frees = vector_frees;
    nvmet_tcp_free_cmd_buffers(&cmd);
    CHECK(sg_frees == old_sg_frees && vector_frees == old_vector_frees);
}

static void check_digests(void)
{
    unsigned char data[12288];
    struct scatterlist sg[3];
    struct nvmet_tcp_queue queue = {0};
    struct nvmet_tcp_cmd cmd = { .queue = &queue, .req = {
        .sg = sg, .sg_cnt = 3, .transfer_len = sizeof(data) } };
    for (size_t i = 0; i < sizeof(data); i++) data[i] = i * 37 + i / 257;
    for (unsigned int i = 0; i < 3; i++)
        sg[i] = (struct scatterlist){ .length = PAGE_SIZE,
            .last = i == 2, .data = data + i * PAGE_SIZE };
    CHECK((u32)~crc32c(~0U, (const unsigned char *)"123456789", 9) == 0xe3069283U);
    const unsigned int offsets[] = { 0, 1000, 3000, 11192 };
    const unsigned int lengths[] = { 1000, 2000, 8192, 1096 };
    for (unsigned int i = 0; i < 4; i++) {
        cmd.pdu_len = lengths[i];
        cmd.rbytes_done = offsets[i] + lengths[i];
        u32 expected = ~crc32c(~0U, data + offsets[i], lengths[i]);
        nvmet_tcp_prep_recv_ddgst(&cmd);
        CHECK(cmd.exp_ddgst == expected);
        CHECK(queue.offset == 0 && queue.left == 4);
        CHECK(queue.rcv_state == NVMET_TCP_RECV_DDGST);
        unsigned int outside = offsets[i] ? 0 : sizeof(data) - 1;
        data[outside] ^= 0xff;
        nvmet_tcp_prep_recv_ddgst(&cmd);
        CHECK(cmd.exp_ddgst == expected);
        data[outside] ^= 0xff;
    }
    observe_send_digest(&cmd);
    CHECK(cmd.exp_ddgst == (u32)~crc32c(~0U, data, sizeof(data)));
}

int main(void)
{
    const unsigned int totals[] = { 4096, 8192, 4 * 1024 * 1024 };
    const unsigned int offsets[] = { 0, 4, 512, 1000, 3000, 4092, 4096 };
    const unsigned int lengths[] = { 4, 512, 1096, 2000, 4096, 8192 };
    unsigned int spans = 0;
    for (size_t t = 0; t < sizeof(totals) / sizeof(totals[0]); t++)
        for (size_t o = 0; o < sizeof(offsets) / sizeof(offsets[0]); o++)
            for (size_t n = 0; n < sizeof(lengths) / sizeof(lengths[0]); n++)
                if (offsets[o] < totals[t] && lengths[n] <= totals[t] - offsets[o]) {
                    check_span(totals[t], offsets[o], lengths[n]);
                    spans++;
                }
    check_span(4 * 1024 * 1024, 0, 4 * 1024 * 1024);
    spans++;

    struct scatterlist sg[] = { { .length = 4096, .last = true } };
    struct bio_vec iov[1] = {0};
    struct nvmet_tcp_cmd cmd = { .iov = iov, .req = { .sg = sg }, .pdu_len = 4 };
    vectors_built = 0;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == -EPROTO);
    CHECK(vectors_built == 0);

    check_digests();
    cmd.req.sg_cnt = 1;
    cmd.rbytes_done = 4096;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == -EPROTO);
    CHECK(vectors_built == 0);
    cmd.rbytes_done = 0;
    cmd.pdu_len = 4100;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == -EPROTO);
    sg[0].length = 0;
    cmd.pdu_len = 4;
    vectors_built = 0;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == -EPROTO);
    CHECK(vectors_built == 0);
    sg[0].length = 512;
    cmd.rbytes_done = 512;
    CHECK(nvmet_tcp_build_pdu_iovec(&cmd) == -EPROTO);
    CHECK(vectors_built == 0);

    printf("%u spans; %u checks; %u failures (isolated source, stubbed kernel APIs)\n",
           spans, checks, failures);
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    path = "drivers/nvme/target/tcp.c"
    source = (subprocess.check_output(
        ["git", "-C", str(args.repo), "show", args.revision + ":" + path], text=True
    ) if args.revision else (args.repo / path).read_text())
    send_digest = re.search(r"nvmet_tcp_calc_ddgst\(.*?\);",
                           function(source, "nvmet_setup_c2h_data_pdu"), re.S).group()
    harness = "\n".join([PRELUDE, function(source, "nvmet_tcp_build_pdu_iovec"),
                         function(source, "nvmet_tcp_free_cmd_buffers"),
                         function(source, "nvmet_tcp_calc_ddgst"),
                         function(source, "nvmet_tcp_prep_recv_ddgst"),
                         "static void observe_send_digest(struct nvmet_tcp_cmd *cmd) { "
                         + send_digest + " }", TESTS])
    args.artifacts.mkdir(parents=True, exist_ok=True)
    source_file = args.artifacts / "harness.c"
    source_file.write_text(harness)
    binary = args.artifacts.resolve() / "harness"
    cc = os.environ.get("CC", "cc")
    command = [cc, "-std=gnu11", "-Wall", "-Werror", "-fsanitize=address,undefined",
               "-fno-omit-frame-pointer", str(source_file), "-o", str(binary)]
    metadata = {
        "revision": args.revision or "working-tree",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "compile_command": command,
        "compiler": subprocess.check_output([cc, "--version"], text=True),
        "limitations": "Kernel iterator, allocation and scatterlist APIs are stubbed; "
                       "CRC32C uses a bytewise reference implementation. "
                       "No sockets, kernel CRC implementation, concurrency or live I/O.",
    }
    (args.artifacts / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    subprocess.run(command, check=True, timeout=60)
    result = subprocess.run([str(binary)], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=30)
    (args.artifacts / "result.log").write_text(result.stdout)
    print(result.stdout, end="")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
