# Governed memory orchestration

This directory implements authority-first lexical recall plus optional current
code evidence. `config.json` has two bounded lanes: governance and evidence.

The normal path never calls a model, starts a provider, or promotes a candidate.
Governance is authoritative; CRG results are rebuildable, revision-bound
evidence; candidate lifecycle remains human reviewed.

The retired MemOS behavioral provider failed valid R7 and R8 quality gates.
Its implementation and live state are intentionally absent. Audit evidence is
preserved outside the production runtime in the owner-only retirement archive.
