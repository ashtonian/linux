#!/bin/bash
set -euo pipefail
cd /work/linux
export ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu-
for variant in production debug; do
    export EXTRAVERSION="-$variant"
    build_output="/work/qualification/config-$variant"
    mkdir -p "$build_output"
    make O="$build_output" defconfig
    for fragment in debian_amd64.config truenas.config "tn-$variant.config"; do
        scripts/kconfig/merge_config.sh -O "$build_output" \
            "$build_output/.config" "scripts/package/truenas/$fragment"
    done
    make O="$build_output" syncconfig
    sha256sum "$build_output/.config"
done
