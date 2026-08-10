# Darwin ARM64 native qualification

Sanitized, metadata-only evidence for the independently attested Darwin ARM64
MemOS 2.0.14 lexical distribution. The packet includes the complete
path/type/size/digest manifests needed to reproduce the two-root attestation.
Raw `.trace` bundles and syscall XML remain temporary because they contain
unnecessary symbol, address, and host-path metadata. The final senior review
checked those retained raw exports against the normalized observations before
accepting the packet. `independent-trace-review.json` identifies that review
and binds every owner-retained raw trace/XML/output digest to the normalized
packet. Repository tests reproduce the full manifest comparison and validate
the durable review binding.

The qualification launcher closes inherited non-standard descriptors before
starting `xctrace`, preventing a pre-connected operator socket from becoming
an unobserved path. The sandbox denies network operations, and candidate
traces fail on network socket creation, connect/send families, or child-process
creation.

The 2.0.10 runtime is used only for isolated schema creation, health/session lifecycle, and rollback health. No 2.0.10 retrieval or MiniLM behavior is accepted as qualification evidence. All retrieval and benchmark evidence is from the 2.0.14 lexical/FTS profile. Production remains immutable 2.0.10, off. R8 was not run.
