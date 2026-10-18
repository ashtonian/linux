#!/bin/bash
set -euo pipefail
variant=${1:?production or debug}
case "$variant" in production|debug) ;; *) exit 2 ;; esac
cd "/work/package-$variant"
export ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- EXTRAVERSION="-$variant"
export PYTHON=python3
export KDEB_COMPRESS="${KDEB_COMPRESS:-gzip}"
# This builder is disposable; the wrapper only adds -z1 to archive creation.
install -m755 /work/u3-runtime/dpkg-deb-local /usr/local/bin/dpkg-deb
# Host tools use native libraries; the cross compiler resolves target libraries.
make archprepare
scripts/package/mkdebian
make -j6 bindeb-pkg
