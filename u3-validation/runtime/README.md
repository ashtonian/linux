# U3 package and guest qualification

These scripts are for the lane's disposable Linux build container and QEMU guest.
They must not be run against an existing target. The guest has no external network
interface, no host device passthrough and no storage credentials. The only NVMe
backend is a newly created 64 MiB QEMU raw file with serial `U3TEST`. The buffered file
namespace uses guest tmpfs. `guest_checks.py` checks the guest marker and backend
serial before configuring targets or issuing writes.

The candidate is `9d9d9d7bd620d4c8ef6358e163ca78949e11c2f2`. Production/debug
configuration follows the three fragment merges in that candidate's CI workflow.
The local builder cross-compiles x86_64 on Ubuntu 22.04/aarch64, retaining the
TrueNAS package scripts. This is package/driver qualification, not a full TrueNAS
appliance-image or middleware test.

`pdu_proxy.py` only binds loopback. It verifies received CRCs and splits each
H2CData PDU into deterministic dword-aligned fragments, regenerating header/data
CRCs. Patterns include 1000/2000/8192/1096 bytes and 512/4096/7680 bytes for a
12 KiB command. Native Linux nvme-tcp still performs controller setup, submission,
C2H digest validation and completion. JSON wire logs record actual command sizes
and split layouts. This is a focused regression reproducer, not a fuzzer.

`guest_checks.py` covers port values 0/1/6/8/10/13/255, invalid input preservation,
active-port rejection, 12 KiB and 1 MiB read/write checksums with both split
patterns and digests on/off, 2 MiB direct block I/O split into bounded NVMe
commands at both 1 MiB and configured 256 KiB limits, passthrough MDTS below the transport ceiling, and
transport module teardown. Flush is exercised but volatile guest/QEMU storage
cannot establish durable persistence. TLS, high load, concurrent mutation,
aggregate memory limits, production failover and fencing are outside this suite.

Production and debug package builds pass, and each final guest reports 36 result
records with complete PASS. [The manifest](../qualification/summary.json)
pins the tested artifacts and links reports, warnings and limitations.

## Reproduction layout

Use a case-sensitive Linux filesystem. The recorded builder uses Ubuntu 22.04
on aarch64, `gcc-x86-64-linux-gnu`, `binutils-x86-64-linux-gnu`, the kernel CI
dependencies, and amd64 development libraries for cross-built perf. Native
OpenSSL libraries remain available for host tools. Do not globally redirect
pkg-config to amd64 libraries: `scripts/sign-file` is a native executable.
The package inventory and perf feature output accompany the build results.
`build-packages.sh` defaults the existing `KDEB_COMPRESS` option to gzip and
installs `dpkg-deb-local` in this disposable builder's `/usr/local/bin`. The
wrapper adds `-z1` only to archive creation; default compression was too slow
for the large symbol packages. This changes Debian archive encoding, not kernel
configuration or source. Do not install this wrapper into a shared host.

1. Export the pinned candidate's full source to `/work/linux`; copy these scripts
   to `/work/u3-runtime`. Run `prepare-package-configs.sh` to independently merge
   the Debian, TrueNAS and variant fragments in the upstream CI order.
2. Make separate full source copies at `/work/package-production` and
   `/work/package-debug`, without `.git` metadata or earlier build outputs.
   Copy each `/work/qualification/config-<variant>/.config` into its source copy.
3. Run `build-packages.sh production` and `build-packages.sh debug`, recording
   their exit codes in `/work/qualification/<variant>-package.exit`. The script
   generates Debian metadata and the required certificate path before packaging.
   Keep the logs at `/work/qualification/<variant>-package.log`.
4. Export an Ubuntu 22.04 **amd64** container root filesystem to
   `/work/guest-root`. Install `python3 nvme-cli iproute2 kmod cpio busybox-static
   ca-certificates` in that container before exporting it. The builder needs
   native `qemu-system-x86`, `cpio`, `gzip` and `kmod`.
5. Run `boot-package.sh production` and `boot-package.sh debug` sequentially.
   Each extracts its packaged image, restores Ubuntu's usr-merge symlink, keeps
   the NVMe module dependency closure, and creates a fresh emulated namespace.
   `edd=off` bypasses the observed SeaBIOS disk-probe stall. The guest console
   must be a character device, not a Docker-export placeholder file.
6. Run `collect-packages.py` after both package builds finish. Preserve the
   manifests, configuration files, logs and extracted guest results. The boot
   script returns success only after the guest's complete PASS report is decoded.

The QEMU invocation exposes no network interface or host device. Module loading
and I/O happen only inside that VM. Package maintainer scripts are not installed
into or executed against the host. The root filesystem is an initramfs, not a
TrueNAS appliance image; TLS and middleware services are not configured.
