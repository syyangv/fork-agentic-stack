# Native Intel external qualification bundle v1

Decision: **Path A selected — preserve the strict zero-attempt evidence standard and use a native Intel x86_64 Mac.**

Current state: **BLOCKED PENDING AN EXTERNAL NATIVE INTEL HOST.** No external qualification has run; Phase 9A remains pending and R8 remains unauthorized.

Portable owner-safe archive:

- Path: `/private/tmp/phase9a-intel-bundle-final2-20260810/phase9a-intel-qualification-v1.tar.gz`
- SHA-256: `bd2b48953faebff6ca9bca1bd95b0cc7dc685bfbf0945109c713a880292b58ad`
- Adjacent checksum: `/private/tmp/phase9a-intel-bundle-final2-20260810/phase9a-intel-qualification-v1.tar.gz.sha256`
- Size: approximately 2.8 MiB
- Internal manifest: 23 files, all verified

The archive includes only the pinned 2.0.14 and 2.0.10 npm artifacts; reviewed lexical installer/locks/transform metadata; canonical application qualification script and required orchestration modules; SIP-preserving deny-network profile; Apple System Call Trace wrapper; direct and child-process canaries; strict syscall parser; evidence schema; sanitized hardware/tool attestation; and exact operator instructions.

The runner fails closed unless the host is native Darwin x86_64, not Rosetta-translated, SIP-enabled, using macOS 14.5 build 23F79, x86_64 Node v22.22.2/npm 10.9.7, and Xcode 16.0 (16C5032a) System Call Trace. Direct and child canaries must both observe sandbox-denied `socket(AF_INET)`, `connect`, and `sendto` before MemOS runs. Artifact, source, lock, immutable manifest, tool, platform, empty-trace, application, and network-attempt mismatches all stop execution.

Dependency installation is the only setup stage permitted network access. Canary and qualification workloads run under `sandbox-exec` with `deny network*` and a credential-free `env -i`. The canonical workload covers fresh 2.0.14, copied 2.0.10 opened by 2.0.14, and restored 2.0.10 opened by 2.0.14. It neither accesses live runtime nor reuses R7/R8 material.

Raw Xcode traces stay owner-local because their TOC inventories unrelated host process names. The return packet contains target-only syscall XML, sanitized summaries, host attestation without serial identifiers, qualification JSON, and checksums. A passing result still requires independent review and does not authorize merge, deployment, activation, or R8.

Local validation: archive checksum passed; internal 23/23 checksums passed; Python compilation and shell syntax passed; execution on this Apple Silicon host stopped at the required native-Intel platform gate.
