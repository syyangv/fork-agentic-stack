# Evidence ledger

`ledger.jsonl` is the portable, append-only ledger for bounded structural and
executed-test provenance. Records contain identifiers, hashes, summaries, and
verification outcomes only—never prompts, source bodies, environments, or raw
CRG/tool output.

CRG discovery remains tool-mediated. Generate a request with
`memory_orchestrate.py evidence request`, call the named CRG MCP tool, then
submit a bounded result with `evidence record`. Candidate `TESTED_BY` graph
edges are structural associations and never count as executed tests; only an
explicit `evidence record-test` entry does.

The ledger is created with owner-only permissions and duplicate evidence IDs
are suppressed under a cross-process lock.
