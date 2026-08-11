# Behavioral provider rejection

MemOS is not part of the production architecture.

| Experiment | Outcome |
|---|---|
| R7 | Valid; equal task success; approximately 2.54% slower median |
| R8 | Valid; 18/20 success in both arms; 5.71% slower median; 5.97% worse p95; zero recoveries |

The compatibility/privacy qualification passed, but the adoption gate required
measurable execution benefit. Two independent evaluations found none. MemOS
provider, bridge, runtime, lifecycle capture, assist/evolution, promotion,
installer, export, and operational surfaces were therefore retired.

The complete private evidence is preserved owner-only at:

`~/.agent/archives/memos-retirement-20260811T020000Z`

Outer archive SHA-256:

`7bf3ea62a46297fd527fff5b3d4bc3409016a9fb5d7f339b9a1d3c6f86fbd539`

The archive contains its own index, nested hashes, and restore/read procedure.
It is historical evidence only and grants no activation authority.
