# Process-level trace attempt v1

Result: **BLOCKED BEFORE WORKLOAD TRACE**. Phase 9A remains pending and R8 was not run.

The authorized macOS `dtruss` attempt did not start the qualification process: SIP rejected execution of `/usr/bin/sandbox-exec` with `Operation not permitted`. SIP was not and must never be disabled for this qualification.

The safe Linux alternative stopped at its immutable-runtime prerequisite. In the disposable Debian 12 / arm64 container, the exact pinned 2.0.14 tarball, reviewed lexical transform, lexical lock, and package hash were used. The install produced manifest `532c042...`, not the reviewed Darwin candidate manifest `dc0aae...`. The only file differences were the platform-native `better_sqlite3.node` and `esbuild` binaries, but those binaries are part of the runtime being qualified. Therefore the Linux runtime is not execution-equivalent to the attested Darwin runtime.

Per the fail-closed plan, no workload `strace`, deny canary, qualification assertion, deployment, activation, or R8 run followed. The Linux environment is recorded only as blocker evidence; it does not supersede the Darwin manifest or qualify zero egress.

See `blocker.json` for structured hashes and the two differing file records. Evidence contains syscall metadata/tool output and file hashes only; no payloads, credentials, runtime databases, or user data.
