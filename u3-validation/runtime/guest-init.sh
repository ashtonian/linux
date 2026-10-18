#!/bin/bash
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
mkdir -p /dev/pts /tmp /run /results /sys/kernel/config
mount -t devpts devpts /dev/pts
mount -t tmpfs -o size=256m tmpfs /tmp
mount -t tmpfs tmpfs /run
hostname u3-disposable
ip link set lo up
modprobe nvme
modprobe nvmet
modprobe nvmet_tcp
modprobe nvme_tcp
mount -t configfs configfs /sys/kernel/config
python3 /u3/guest_checks.py 2>&1 | tee /results/checks.log
status=${PIPESTATUS[0]}
dmesg > /results/dmesg.log
cat /results/dmesg.log
printf 'U3_GUEST_EXIT=%s\n' "$status"
# Export small text evidence over the serial console without enabling guest networking.
tar -C /results -czf /tmp/u3-results.tgz --exclude=write.bin --exclude=read.bin .
echo U3_ARTIFACT_BEGIN
base64 -w 76 /tmp/u3-results.tgz
echo U3_ARTIFACT_END
sync
/bin/busybox poweroff -f
