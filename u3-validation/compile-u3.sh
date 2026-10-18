#!/bin/bash
set -euo pipefail

cd /work/linux
export ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- EXTRAVERSION=-production
make O=/work/output W=1 -j6 \
	drivers/nvme/target/core.o \
	drivers/nvme/target/configfs.o \
	drivers/nvme/target/admin-cmd.o \
	drivers/nvme/target/tcp.o \
	drivers/nvme/target/passthru.o \
	drivers/nvme/target/zns.o \
	drivers/nvme/target/rdma.o
make O=/work/output W=1 -j6 drivers/nvme/target/
