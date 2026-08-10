# Darwin ARM64 native qualification

Sanitized, metadata-only evidence for the independently attested Darwin ARM64 MemOS 2.0.14 lexical distribution. Raw `.trace` bundles and syscall XML remain temporary and are intentionally not committed because they contain unnecessary symbol, address, and host-path metadata.

The 2.0.10 runtime is used only for isolated schema creation, health/session lifecycle, and rollback health. No 2.0.10 retrieval or MiniLM behavior is accepted as qualification evidence. All retrieval and benchmark evidence is from the 2.0.14 lexical/FTS profile. Production remains immutable 2.0.10, off. R8 was not run.
