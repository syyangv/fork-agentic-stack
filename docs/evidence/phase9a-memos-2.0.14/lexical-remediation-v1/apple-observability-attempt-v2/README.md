# Apple observability attempt v2

Result: **BLOCKED BY EXACT-RUNTIME PLATFORM LIMITATION**. Phase 9A remains pending; R8 was not run.

Apple Xcode 16 `System Call Trace` works under SIP when `xctrace` is forced to native arm64. A direct-target deny-network canary observed the expected `socket(AF_INET)`, denied `connect`, and denied `sendto` syscalls. A child-only canary was also denied, but the process-scoped trace recorded none of the child's network syscalls, proving that `target-pid=SINGLE` does not cover the process tree required by the existing Python-to-Node runner.

More importantly, the exact reviewed MemOS runtime manifest `dc0aae...` contains x86_64 native artifacts and is executed by the reviewed x86_64 Node route through Rosetta 2 on this Apple Silicon Mac. Apple's tracing backend rejects that exact execution mode with `ktrace cannot trace the system under Rosetta translation`. Rebuilding arm64 would create a different immutable runtime and cannot qualify the reviewed candidate.

The Network Connections instrument and unified sandbox violation logs are corroborative but not exhaustive for short denied syscall attempts. `fs_usage`/`sc_usage` require broader privilege; sampling tools cannot prove absence. SIP remained enabled; no kernel extension, entitlement change, unrelated traffic capture, deployment, activation, or R8 occurred.

No policy tradeoff was selected. Closure now requires either (1) separately approved use of a native x86_64 macOS test host/tool entitlement that can run the exact attested runtime, or (2) explicit human acceptance of a revised evidence standard. Raw `.trace` bundles remain ephemeral because their table of contents inventories unrelated host process names; the durable packet retains their tree digests plus sanitized target-only syscall summaries.
