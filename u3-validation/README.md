# NVMe target transfer-limit regression evidence

Candidate: `9d9d9d7bd620d4c8ef6358e163ca78949e11c2f2` on
[`work/u3-upstream-aligned`](https://github.com/ashtonian/linux/tree/work/u3-upstream-aligned).
Base: TrueNAS `truenas/linux-6.18` at
`e43b65255bec4614c14447eb6152711e3e6e8124`.
This evidence branch adds only `u3-validation/` to that candidate. It is separate
from the proposed kernel PR.

The candidate advertises a TCP maximum of 1 MiB using Alfonso Kuen's reviewed
v3 patch. The existing per-command allocation and H2CData PDU bounds remain
4 MiB, independently named. Port `param_mdts` can lower the advertised limit;
passthrough preserves lower backend limits. Advertisement does not enforce a
smaller per-command rejection threshold. No performance optimum is claimed.

## Recorded results

- 154 MDTS, port-setting and allocation-boundary assertions passed under Linux
  ASan/UBSan. The unchanged base fails 30 of these expectations.
- Another 154 assertions passed with only the isolated H2CData constant reduced
  to 1 MiB, checking independence from advertisement and command allocation.
- 927 vector, per-PDU digest and cleanup assertions passed across 100 spans,
  including a 4096-byte PDU starting 512 bytes into a command buffer and a command
  split into 1000/2000/8192/1096-byte PDUs.
- Seven affected target objects and the configured target directory compiled
  with `W=1`; configured module aggregates linked as relocatable objects.
- Strict checkpatch on the cumulative source diff reported no findings.

[summary.json](summary.json) pins source/config/patch hashes and records limits.
The two build logs cover the initial seven-object build and the subsequent
configfs formatting correction plus complete target-directory build. Compiler
metadata and individual results accompany each harness.

These harnesses extract real source functions but stub kernel allocation,
scatterlist, iterator, locking and parser APIs. CRC32C uses a bytewise reference
implementation. This is not kernel, socket or storage integration testing.
Full package builds, modpost/module loading, boot, real configfs/Identify,
network digest/TLS, command tracing, pressure and durability checks are NOT RUN.

## Reproduce isolated checks

Use a disposable Linux environment with Python 3 and a C compiler supporting
AddressSanitizer and UndefinedBehaviorSanitizer. From this checkout:

```sh
python3 u3-validation/check_mdts.py --repo . --artifacts /tmp/u3-mdts
python3 u3-validation/check_mdts.py --repo . --alter-pdu-limit --artifacts /tmp/u3-pdu
python3 u3-validation/check_tcp_iovec.py --repo . --artifacts /tmp/u3-fragments
```

For the expected failing control, ensure the pinned base exists locally:

```sh
python3 u3-validation/check_mdts.py --repo . \
  --revision e43b65255bec4614c14447eb6152711e3e6e8124 \
  --artifacts /tmp/u3-baseline
```

Expected result: 30 failures out of 154 assertions and exit status 1. The base
has no port MDTS store, so the harness substitutes an ENOENT stub for that missing
function. This is a feature regression control, not 30 observed runtime faults.

## Reproduce the target build

The recorded environment is Ubuntu 22.04/aarch64, with GCC 11.4 cross-compiling
x86_64 and the production configuration fragments from the pinned TrueNAS tree.
Use a disposable container with a case-sensitive volume mounted at `/work` and
no host devices or storage credentials. `prepare-linux-build.sh` installs build
dependencies, downloads the pinned base and prepares `/work/output/.config`.
It must run inside that container; it is not a host setup script. Package mirrors
can change, so compare compiler and config hashes when reproducing.

Generate the candidate patch from this checkout, copy it and the two build
scripts into `/work` in the container, then run inside the container:

```sh
# Generate this patch in the checkout before copying it into the container:
git diff e43b65255bec4614c14447eb6152711e3e6e8124 \
  9d9d9d7bd620d4c8ef6358e163ca78949e11c2f2 > u3.patch

# Inside the disposable build container:
bash /work/prepare-linux-build.sh
git -C /work/linux apply /work/u3.patch
bash /work/compile-u3.sh
```

This builds the configured target directory, not full Debian kernel packages.
No target is configured or live storage accessed by these scripts.
