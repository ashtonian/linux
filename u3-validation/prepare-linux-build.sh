#!/bin/bash
set -euo pipefail

kernel_sha=e43b65255bec4614c14447eb6152711e3e6e8124

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
	bc bison build-essential ca-certificates cpio curl dwarves flex gawk git \
	gcc-x86-64-linux-gnu libelf-dev libssl-dev make python3 rsync zstd

mkdir -p /work/linux /work/output
curl --fail --location --retry 3 --connect-timeout 15 --max-time 600 \
	"https://codeload.github.com/truenas/linux/tar.gz/$kernel_sha" \
	--output /work/linux.tar.gz
sha256sum /work/linux.tar.gz
tar --extract --gzip --file /work/linux.tar.gz \
	--directory /work/linux --strip-components=1

cd /work/linux
export ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- EXTRAVERSION=-production
make O=/work/output defconfig
scripts/kconfig/merge_config.sh -m -O /work/output \
	/work/output/.config \
	scripts/package/truenas/debian_amd64.config \
	scripts/package/truenas/truenas.config \
	scripts/package/truenas/tn-production.config
make O=/work/output olddefconfig
sha256sum /work/output/.config
x86_64-linux-gnu-gcc --version
make --version
pahole --version
df -h /work
