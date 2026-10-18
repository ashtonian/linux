#!/usr/bin/env python3
"""Compile isolated kernel source functions against inert userspace stubs.

This checks MDTS composition, configfs store state, and allocation ordering.
It does not exercise kernel APIs, real configfs, sockets, hardware or storage.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def function(source, name, optional=False):
    match = re.search(r"^[^\n;{}]*\b" + name + r"\s*\([^;]*?\)\s*\{", source, re.M)
    if match is None:
        if optional:
            return ""
        raise ValueError(f"Function not found: {name}")
    depth = 1
    end = match.end()
    while depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    return source[match.start():end]


PRELUDE = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <sys/types.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define NVME_CAP_MPSMIN(cap) (((cap) >> 48) & 15)
#define ilog2(value) (63 - __builtin_clzll((unsigned long long)(value)))
#define min_not_zero(a, b) ({ __auto_type x = (a); __auto_type y = (b); \
    !x ? y : !y ? x : x < y ? x : y; })
#define le32_to_cpu(value) (value)
#define NVME_STATUS_DNR 0x4000
#define NVME_SC_SGL_INVALID_DATA 1
#define NVME_SC_INVALID_FIELD 2
#define NVME_SC_SGL_INVALID_OFFSET 3
#define NVME_SC_INTERNAL 4
#define NVME_SC_CONNECT_INVALID_PARAM 5
#define NVME_SC_CONNECT_CTRL_BUSY 6
#define NVME_SGL_FMT_DATA_DESC 0
#define NVME_SGL_FMT_OFFSET 1
#define GFP_KERNEL 0
#define __GFP_NOWARN 0
#define NVMET_MAX_MDTS 255
#define SZ_4M (4U * 1024 * 1024)
#define NVMET_TCP_BACKLOG 128
#define NVMET_TCP_Q_DISCONNECTING 2
#define pr_err(...) ((void)0)

struct nvmet_ctrl;
struct nvmet_fabrics_ops { u8 (*get_mdts)(const struct nvmet_ctrl *); };
struct nvmet_ctrl { uint64_t cap; struct nvmet_fabrics_ops *ops; };
struct nvmet_port { int mdts, inline_data_size; bool enabled; };
struct config_item { struct nvmet_port *port; };
struct nvmet_sq { struct nvmet_ctrl *ctrl; int qid, size; };
struct nvme_sgl_desc { u32 length; u8 type; };
struct nvme_command {
    struct { struct { struct nvme_sgl_desc sgl; } dptr; u8 opcode; } common;
};
struct nvmet_req {
    struct nvmet_sq *sq;
    struct nvmet_port *port;
    struct nvme_command *cmd;
    unsigned int transfer_len, sg_cnt;
    void *sg;
};
struct nvmet_tcp_cmd {
    struct nvmet_req req;
    u32 pdu_len, rbytes_done;
    void *cur_sg;
    unsigned long *iov;
};
struct nvmet_tcp_queue { struct nvmet_sq nvme_sq; int state, nr_cmds; };
struct nvme_id_ctrl { u8 mdts; };
static int nvmet_tcp_queue_mutex, nvmet_tcp_queue_list;
static int allocations, iov_allocations, frees, queue_allocations;
static bool fail_sg, fail_iov;
static unsigned long allocation;

#define container_of(ptr, type, member) ((type *)(ptr))
#define list_for_each_entry(pos, head, member) \
    for ((void)(head), (pos) = NULL; (pos); (pos) = NULL)
static void mutex_lock(int *mutex) { (void)mutex; }
static void mutex_unlock(int *mutex) { (void)mutex; }
static int nvmet_tcp_alloc_cmds(struct nvmet_tcp_queue *queue)
{ (void)queue; queue_allocations++; return 0; }
static inline struct nvmet_port *to_nvmet_port(struct config_item *item)
{ return item->port; }
static inline bool nvmet_is_port_enabled(struct nvmet_port *port, const char *caller)
{ (void)caller; return port->enabled; }
static inline int kstrtoint(const char *text, unsigned int base, int *value)
{
    char *end;
    long long parsed;
    errno = 0;
    if (!*text || *text == ' ' || *text == '\t')
        return -EINVAL;
    parsed = strtoll(text, &end, base);
    if (end == text || (*end && strcmp(end, "\n")))
        return -EINVAL;
    if (errno || parsed < INT_MIN || parsed > INT_MAX)
        return -ERANGE;
    *value = parsed;
    return 0;
}
static bool nvme_is_write(const struct nvme_command *cmd)
{ return cmd->common.opcode == 1; }
static void *sgl_alloc(u32 length, int flags, unsigned int *count)
{
    (void)length; (void)flags;
    allocations++;
    *count = 1;
    return fail_sg ? NULL : &allocation;
}
static void *kmalloc_array(size_t count, size_t size, int flags)
{
    (void)count; (void)size; (void)flags;
    iov_allocations++;
    return fail_iov ? NULL : &allocation;
}
static void nvmet_tcp_free_cmd_buffers(struct nvmet_tcp_cmd *cmd)
{ (void)cmd; frees++; }
"""


TESTS = r"""
static unsigned int checks, failures;
#define CHECK(condition) do { checks++; if (!(condition)) { \
    fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition); failures++; \
} } while (0)

int main(void)
{
    struct nvmet_fabrics_ops ops = { .get_mdts = TCP_CALLBACK };
    struct nvmet_ctrl ctrl = { .ops = &ops };
    struct nvmet_tcp_queue queue = { .nvme_sq = { .ctrl = &ctrl, .size = 8 } };
    struct nvmet_port port = { .inline_data_size = 4096 };
    struct nvmet_req req = { .sq = &queue.nvme_sq, .port = &port };
    struct config_item item = { .port = &port };
    const int ports[] = { 0, 1, 6, 8, 9, 10, 13, 255 };
    const int expected[] = { 8, 1, 6, 8, 8, 8, 8, 8 };
    const char *invalid[] = {
        "-1", "256", "2147483648", "-2147483649", "bad", "", "8.5"
    };
    const char *valid[] = { "0", "1", "8", "10", "255", "0x8", "8\n" };
    const int stored[] = { 0, 1, 8, 10, 255, 8, 8 };

    CHECK(NVMET_TCP_MAXH2CDATA == TEST_PDU_LIMIT);
    for (size_t i = 0; i < sizeof(ports) / sizeof(ports[0]); i++) {
        port.mdts = ports[i];
        CHECK(observe_identify(&req) == expected[i]);
        CHECK(observe_passthru(&req, 1U << 18) == expected[i]);
        CHECK(observe_passthru(&req, 1U << 8) ==
              (expected[i] < 5 ? expected[i] : 5));
    }

    /* A transport without a limit must retain its configured port value. */
    ops.get_mdts = NULL;
    port.mdts = 0;
    CHECK(observe_identify(&req) == 0);
    port.mdts = 8;
    CHECK(observe_identify(&req) == 8);

    ops.get_mdts = TCP_CALLBACK;
    port.mdts = 0;
    queue_allocations = 0;
    CHECK(nvmet_tcp_install_queue(&queue.nvme_sq) == 0);
    CHECK(queue_allocations == 1);

    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        port.mdts = 8;
        CHECK(nvmet_param_mdts_store(&item, invalid[i], strlen(invalid[i])) < 0);
        CHECK(port.mdts == 8);
    }
    for (size_t i = 0; i < sizeof(valid) / sizeof(valid[0]); i++) {
        CHECK(nvmet_param_mdts_store(&item, valid[i], strlen(valid[i])) ==
              (ssize_t)strlen(valid[i]));
        CHECK(port.mdts == stored[i]);
    }
    port.enabled = true;
    port.mdts = 8;
    CHECK(nvmet_param_mdts_store(&item, "10", 2) == -EACCES);
    CHECK(port.mdts == 8);
    port.enabled = false;

    for (unsigned int write = 0; write <= 1; write++) {
        for (unsigned int inline_data = 0; inline_data <= 1; inline_data++) {
            const u32 sizes[] = { 0, 4096, 4097, 1048576, 1049088, 2097152, 4194304, 4194305, 4194816, UINT32_MAX };
            for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
                struct nvme_command command = { .common = {
                    .opcode = write, .dptr.sgl = {
                        .length = sizes[i], .type = inline_data ? 1 : 0x50
                    }
                } };
                struct nvmet_tcp_cmd cmd = { .req = {
                    .cmd = &command, .port = &port
                } };
                bool accepted = !sizes[i] || (sizes[i] <= 4194304 &&
                    (!inline_data || (write && sizes[i] <= 4096)));
                allocations = iov_allocations = 0;
                CHECK((nvmet_tcp_map_data(&cmd) == 0) == accepted);
                CHECK(allocations == (accepted && sizes[i] ? 1 : 0));
                if (sizes[i] > 4194304)
                    CHECK(iov_allocations == 0);
            }
        }
    }

    struct nvme_command command = { .common = {
        .opcode = 1, .dptr.sgl = { .length = 4096, .type = 0x50 }
    } };
    struct nvmet_tcp_cmd cmd = { .req = { .cmd = &command, .port = &port } };
    fail_sg = true;
    CHECK(nvmet_tcp_map_data(&cmd) == NVME_SC_INTERNAL);
    fail_sg = false;
    fail_iov = true;
    frees = 0;
    CHECK(nvmet_tcp_map_data(&cmd) == NVME_SC_INTERNAL);
    CHECK(frees == 1);

    printf("%u checks; %u failures (isolated source functions, stubbed kernel APIs)\n",
           checks, failures);
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--revision", help="Read this Git revision instead of the working tree")
    parser.add_argument("--alter-pdu-limit", action="store_true",
                        help="Change only the isolated PDU constant to 1 MiB; command expectations stay 4 MiB")
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    sources = {}
    for name in ("tcp.c", "nvmet.h", "admin-cmd.c", "configfs.c", "passthru.c"):
        path = "drivers/nvme/target/" + name
        if args.revision:
            sources[name] = subprocess.check_output(
                ["git", "-C", str(args.repo), "show", args.revision + ":" + path], text=True
            )
        else:
            sources[name] = (args.repo / path).read_text()

    tcp = sources["tcp.c"]
    callback = function(tcp, "nvmet_tcp_get_mdts", optional=True)
    wired = bool(re.search(r"\.get_mdts\s*=\s*nvmet_tcp_get_mdts", tcp))
    helper = function(sources["nvmet.h"], "nvmet_ctrl_mdts", optional=True)
    store = function(sources["configfs.c"], "nvmet_param_mdts_store", optional=True)
    if not store:
        store = """static ssize_t nvmet_param_mdts_store(struct config_item *item,
            const char *page, size_t count) {
            (void)item; (void)page; (void)count; return -ENOENT;
        }"""
    identify = re.search(
        r"(?:/\* Limit MDTS according[^\n]*\*/\s*)?"
        r"(?:if \(ctrl->ops->get_mdts\).*?|id->mdts = nvmet_ctrl_mdts\(req\);)"
        r"(?=\s*id->cntlid =)", sources["admin-cmd.c"], re.S
    ).group()
    passthru = re.search(
        r"page_shift = NVME_CAP_MPSMIN.*?(?=\s*id->acl =)", sources["passthru.c"], re.S
    ).group()
    wrappers = """
static u8 observe_identify(struct nvmet_req *req) {
    struct nvmet_ctrl *ctrl = req->sq->ctrl;
    struct nvme_id_ctrl result = {0}, *id = &result;
    (void)ctrl;
""" + identify + "\n    return id->mdts;\n}\n" + """
static u8 observe_passthru(struct nvmet_req *req, unsigned int max_hw_sectors) {
    struct nvmet_ctrl *ctrl = req->sq->ctrl;
    struct nvme_id_ctrl result = {0}, *id = &result;
    int page_shift;
""" + passthru + "\n    return id->mdts;\n}\n"
    constants = re.findall(r"^#define NVMET_TCP_(?:MAXH2CDATA|MAX_CMD_DATA|MAX_MDTS)[^\n]+", tcp, re.M)
    pdu_limit = 1024 * 1024 if args.alter_pdu_limit else 4 * 1024 * 1024
    if args.alter_pdu_limit:
        constants = [re.sub(r"^(#define NVMET_TCP_MAXH2CDATA)\s+.*", r"\g<1> 0x100000", item)
                     for item in constants]
    harness = "\n".join([
        PRELUDE, *constants, f"#define TEST_PDU_LIMIT {pdu_limit}", callback, helper, store,
        function(tcp, "nvmet_tcp_has_data_in"),
        function(tcp, "nvmet_tcp_map_data"),
        function(tcp, "nvmet_tcp_install_queue"), wrappers,
        "#define TCP_CALLBACK " + ("nvmet_tcp_get_mdts" if wired else "NULL"), TESTS,
    ])
    source_file = args.artifacts / "harness.c"
    source_file.write_text(harness)
    compiler = os.environ.get("CC", "cc")
    binary = args.artifacts.resolve() / "harness"
    command = [compiler, "-std=gnu11", "-Wall", "-Werror", "-fsanitize=address,undefined",
               "-fno-omit-frame-pointer", str(source_file), "-o", str(binary)]
    metadata = {
        "revision": args.revision or "working-tree",
        "alter_pdu_limit": args.alter_pdu_limit,
        "compiler": subprocess.check_output([compiler, "--version"], text=True),
        "compile_command": command,
        "source_sha256": {name: hashlib.sha256(text.encode()).hexdigest()
                          for name, text in sources.items()},
        "limitations": "Isolated functions and MDTS source fragments; kernel APIs are stubbed. "
                       "No kernel/configfs/transport/storage/runtime qualification.",
    }
    (args.artifacts / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    subprocess.run(command, check=True, timeout=60)
    try:
        result = subprocess.run([str(binary)], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30)
    except subprocess.TimeoutExpired:
        (args.artifacts / "result.log").write_text("BLOCKED: harness timed out after 30 seconds.\n")
        raise SystemExit(124)
    (args.artifacts / "result.log").write_text(result.stdout)
    print(result.stdout, end="")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
