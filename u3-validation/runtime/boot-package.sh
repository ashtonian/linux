#!/bin/bash
set -euo pipefail
variant=${1:?production or debug}
case "$variant" in production|debug) ;; *) exit 2 ;; esac
package_root="/work/package-$variant"
result_root="/work/qualification/$variant"
mkdir -p "$result_root" /work/guest-root/u3
image_deb=$(find /work -maxdepth 1 -type f -name "linux-image-truenas-$variant-amd64_*.deb" -print -quit)
test -n "$image_deb"
dpkg-deb -x "$image_deb" /work/guest-root
# Raw package extraction replaces Ubuntu's /lib symlink; restore usr-merge.
if [ ! -L /work/guest-root/lib ]; then
    cp -a /work/guest-root/lib/. /work/guest-root/usr/lib/
    rm -r /work/guest-root/lib
    ln -s usr/lib /work/guest-root/lib
fi
release=$(cat "$package_root/include/config/kernel.release")
test -f "/work/guest-root/boot/vmlinuz-$release"
cp /work/u3-runtime/guest-init.sh /work/guest-root/init
cp /work/u3-runtime/guest_checks.py /work/u3-runtime/pdu_proxy.py /work/guest-root/u3/
chmod 755 /work/guest-root/init
mkdir -p /work/guest-root/dev /work/guest-root/results
if [ ! -c /work/guest-root/dev/console ]; then
    rm -f /work/guest-root/dev/console
    mknod /work/guest-root/dev/console c 5 1
fi
if [ ! -c /work/guest-root/dev/null ]; then
    rm -f /work/guest-root/dev/null
    mknod /work/guest-root/dev/null c 1 3
fi
depmod -b /work/guest-root "$release"
python3 /work/u3-runtime/trim-guest-modules.py "$release"
# The root filesystem and emulated namespace contain only disposable test data.
truncate -s 0 "$result_root/nvme.raw"
truncate -s 64M "$result_root/nvme.raw"
(cd /work/guest-root && find . -print0 | cpio --null -o --format=newc 2> "$result_root/cpio.log" | gzip -1) > "$result_root/initramfs.gz"
sha256sum "$image_deb" "/work/guest-root/boot/vmlinuz-$release" "$result_root/initramfs.gz" > "$result_root/boot-hashes.txt"
set +e
timeout --signal=TERM --kill-after=15 1200 qemu-system-x86_64 \
    -machine q35,accel=tcg -cpu max -smp 2 -m 3072 \
    -nodefaults -display none -monitor none \
    -serial "file:$result_root/console.log" -no-reboot \
    -kernel "/work/guest-root/boot/vmlinuz-$release" \
    -initrd "$result_root/initramfs.gz" \
    -append 'console=ttyS0 earlyprintk=serial,ttyS0,115200 edd=off loglevel=6 panic=1 rdinit=/init u3_disposable=1' \
    -debugcon "file:$result_root/bios.log" -global isa-debugcon.iobase=0x402 \
    -nic none -drive "file=$result_root/nvme.raw,format=raw,if=none,id=u3nvme" \
    -device nvme,drive=u3nvme,serial=U3TEST,mdts=5 \
    > "$result_root/qemu.log" 2>&1
status=$?
printf '%s\n' "$status" > "$result_root/qemu.exit"
tail -10 "$result_root/qemu.log"
grep 'U3_GUEST_EXIT=' "$result_root/console.log" || true
test "$status" -eq 0
python3 /work/u3-runtime/extract-guest-results.py "$result_root/console.log" "$result_root/results"
