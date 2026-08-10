# Phase 9A native Intel macOS qualification bundle

This owner-safe bundle qualifies the exact reviewed x86_64 MemOS lexical runtime. It does not install into `~/.agent`, deploy, activate, use secrets, reuse R7, or run R8.

## Required host

- Physical/native Intel `x86_64` Mac; Rosetta-translated or virtual architecture mismatches fail closed.
- SIP enabled. Do not disable SIP.
- macOS 14.5 build 23F79 and Xcode `xctrace` 16.0 (16C5032a) with `System Call Trace`.
- Python 3.9+, x86_64 Node v22.22.2, and npm 10.9.7.
- At least 1 GB free space under `/private/tmp`.
- Owner-controlled network access is allowed only during locked dependency installation. Every canary and qualification workload is launched under `(deny network*)`.

## Operator procedure

1. Transfer the `.tar.gz` and its adjacent `.sha256` file to the Intel Mac over an owner-approved channel.
2. Verify the archive before extraction: `shasum -a 256 -c phase9a-intel-qualification-*.tar.gz.sha256`.
3. Extract into a new owner-only directory: `umask 077; tar -xzf phase9a-intel-qualification-*.tar.gz`.
4. Enter the extracted directory and verify every bundled file: `shasum -a 256 -c SHA256SUMS`.
5. Confirm no credentials are exported in the shell. The runner launches traced targets with `env -i` and never reads provider configuration.
6. Run: `python3 run_qualification.py`.
7. The runner first attests Intel hardware, non-Rosetta execution, SIP, x86_64 Node, Xcode, artifacts, locks, reviewed transform inputs, and immutable manifest.
8. It then runs direct and child-process `socket/connect/sendto` canaries. Both must be denied and visible in target-only syscall exports. Missing child coverage stops before MemOS.
9. Only after both canaries pass does it trace fresh 2.0.14, copied-2.0.10, and restored-2.0.10 candidate workloads through the canonical application qualifier. Any network syscall/API attempt, empty trace, framing failure, artifact mismatch, or workload error fails closed.
10. Return only the generated `evidence/` directory plus its `SHA256SUMS`. Keep raw `.trace` bundles owner-local for independent review on the Intel host; they are not imported because Xcode TOCs inventory unrelated host process names.

A passing external run is evidence for senior review only. It does not authorize merge, deployment, activation, or R8.
