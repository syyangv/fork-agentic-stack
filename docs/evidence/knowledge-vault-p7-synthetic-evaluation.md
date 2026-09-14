# P7.2 Synthetic Knowledge Retrieval Evaluation

- **Date:** 2026-09-14
- **Worktree:** `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`
- **Scope:** Synthetic work-vault fixtures only; no real vault, personal vault, `~/.agent/knowledge`, daemon/plugin, credentials, network, or existing database was read or initialized.
- **Interfaces:** `knowledge_core.index.KnowledgeIndex` and `knowledge_core.retrieval.KnowledgeRetriever`

## Fixture corpus

The temporary corpus contained 12 Markdown notes under all four formal directories, with deterministic synthetic IDs, titles, aliases, English/Chinese body terms, project paths (`alpha` and `beta`), short literals, and punctuation cases. The temporary SQLite database was created outside the worktree. No fixture excerpts are retained here.

Deterministic fixture path SHA-256 values:

| Path | SHA-256 |
|---|---|
| `10-Projects/alpha/launch-plan.md` | `74b9afd3c4d69f64e57034651a515bf95303f7ef0e40ff44b53584eae03ed1e9` |
| `10-Projects/alpha/api-migration.md` | `8de0c950c0a376e1dfdb2ec6069023644582e839fe0a69783e1a0f87f78c52d5` |
| `10-Projects/alpha/data-contract.md` | `e5f93f143f03c997af0eb006d55c434a8f4a9810dfa1d6c4ee47da37b089ec26` |
| `10-Projects/beta/observability.md` | `55756003232a938b462be39d9eda6e904f2cb94c56bffe219a350ec39b5471e9` |
| `10-Projects/beta/release-readiness.md` | `d8142ab7d7ed9842154e1f934990781fe566a2ae36405a81c1e07167b477c6e5` |
| `10-Projects/beta/edge-cases.md` | `0c9e65942ccba2d444b7b98d8a18ef35d6faefe41a5a46ba56d030e29189983b` |
| `20-Research/retrieval-benchmarks.md` | `a3bf2b6cd57911c044310047a7aacdef0683efbfa9e7402e185eb6739001a42e` |
| `20-Research/chinese-tokenization.md` | `99741d64b3133ca13ad8205f9e37d0259d1f37a0df2573644e9ae6df5760fee2` |
| `30-Decisions/index-boundary.md` | `09f4c7cb87c6da8799667791916b3e7730cc89577682b0df5f04f7a784420a71` |
| `30-Decisions/provider-routing.md` | `48227b3e35555d7dc976473f23b9202833b4f467840565855448c75b09e41400` |
| `40-Lessons/provenance-hash.md` | `7dca3a57ed0c17defec5ad0216287edb39d699414dd143657ee289e92e5f7046` |
| `40-Lessons/short-literals.md` | `68fae9f7eb3718ffdce7422568f02667bdc4366cea4a19e888f3ab5343a61797` |

Index result: `rebuild`, 12 added, 0 skipped, 0 diagnostics.

## Fixed query set and results

The 20 queries and expected source paths were fixed in the evaluator before indexing and measuring. Retrieval used `limit=5` for every query. `Top-5 hit` is a query-level hit when at least one independently expected path appears in the first five returned hits. `Set recall@5` is expected paths found divided by expected paths. `Location` validates returned path/ref path, vault, SHA-256, and line bounds.

| ID | Kind | Query | Project | Expected paths | Result paths (top 5) | Status | Top-5 hit | Set recall@5 | Location |
|---|---|---|---|---|---|---|---|---|---|
| Q01 | title exact | `Launch Plan` | — | `10-Projects/alpha/launch-plan.md` | `10-Projects/alpha/launch-plan.md` | `ok` | yes | 1/1 | pass |
| Q02 | title contains | `Launch` | — | `10-Projects/alpha/launch-plan.md` | `10-Projects/alpha/launch-plan.md`; `10-Projects/alpha/launch-plan.md` | `ok` | yes | 1/1 | pass |
| Q03 | Chinese alias | `发布计划` | — | `10-Projects/alpha/launch-plan.md` | `10-Projects/alpha/launch-plan.md` | `ok` | yes | 1/1 | pass |
| Q04 | title exact | `API Migration` | — | `10-Projects/alpha/api-migration.md` | `10-Projects/alpha/api-migration.md` | `ok` | yes | 1/1 | pass |
| Q05 | mixed alias | `接口迁移` | — | `10-Projects/alpha/api-migration.md` | `10-Projects/alpha/api-migration.md` | `ok` | yes | 1/1 | pass |
| Q06 | English body | `compatibility matrix` | — | `10-Projects/alpha/api-migration.md` | `10-Projects/alpha/api-migration.md` | `ok` | yes | 1/1 | pass |
| Q07 | Chinese body | `回滚演练` | — | `10-Projects/beta/release-readiness.md` | `10-Projects/beta/release-readiness.md` | `ok` | yes | 1/1 | pass |
| Q08 | cross-project body | `incident handoff` | — | `10-Projects/alpha/launch-plan.md`; `10-Projects/beta/observability.md` | `10-Projects/beta/observability.md`; `10-Projects/alpha/launch-plan.md` | `ok` | yes | 2/2 | pass |
| Q09 | cross-project filtered | `incident handoff` | `beta` | `10-Projects/beta/observability.md` | `10-Projects/beta/observability.md` | `ok` | yes | 1/1 | pass |
| Q10 | Chinese alias | `检索基准` | — | `20-Research/retrieval-benchmarks.md` | `20-Research/retrieval-benchmarks.md` | `ok` | yes | 1/1 | pass |
| Q11 | punctuated body | `Top-5 recall` | — | `20-Research/retrieval-benchmarks.md` | `20-Research/retrieval-benchmarks.md` | `ok` | yes | 1/1 | pass |
| Q12 | mixed punctuated body | `中文/English` | — | `20-Research/retrieval-benchmarks.md` | `20-Research/retrieval-benchmarks.md` | `ok` | yes | 1/1 | pass |
| Q13 | English body | `FTS5 trigram` | — | `20-Research/chinese-tokenization.md` | `20-Research/chinese-tokenization.md` | `ok` | yes | 1/1 | pass |
| Q14 | short Chinese body | `短词` | — | `20-Research/chinese-tokenization.md` | `20-Research/chinese-tokenization.md` | `ok` | yes | 1/1 | pass |
| Q15 | Chinese alias | `索引边界` | — | `30-Decisions/index-boundary.md` | `30-Decisions/index-boundary.md` | `ok` | yes | 1/1 | pass |
| Q16 | mixed body | `work-only` | — | `30-Decisions/index-boundary.md` | `30-Decisions/index-boundary.md` | `ok` | yes | 1/1 | pass |
| Q17 | Chinese alias | `模型路由` | — | `30-Decisions/provider-routing.md` | `30-Decisions/provider-routing.md` | `ok` | yes | 1/1 | pass |
| Q18 | Chinese alias | `来源哈希` | — | `40-Lessons/provenance-hash.md` | `40-Lessons/provenance-hash.md` | `ok` | yes | 1/1 | pass |
| Q19 | short literal | `Q3` | — | `40-Lessons/short-literals.md` | `40-Lessons/short-literals.md` | `ok` | yes | 1/1 | pass |
| Q20 | short literal punctuation | `a_b` | — | `40-Lessons/short-literals.md` | `40-Lessons/short-literals.md` | `ok` | yes | 1/1 | pass |

## Aggregate metrics and gate

- **Top-5 expected-source query hits:** **20/20 (100%)**; average expected-source set recall@5: **100%**.
- **Queries with all expected paths in top-5:** **20/20**.
- **Returned hits checked for source location:** **22/22 correct (100%)**.
- **No-result behavior:** **0/20 `no_results`**; all 20 responses were `ok`.
- **Plan threshold:** **PASS** — 20/20 is at least 16/20, and source-location correctness is 100%.

## Exact commands and regression checks

Evaluation command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/.agent/tools /Users/syang/miniconda3/bin/python3 -B /private/tmp/p7_synthetic_evaluation.py | tee /private/tmp/p7_synthetic_evaluation.json
```

Final knowledge unittest discovery:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/.agent/tools /Users/syang/miniconda3/bin/python3 -B -m unittest discover -s tests -p 'test_knowledge*.py' -v
```

Result: **62 tests, OK**.

Final knowledge `py_compile`:

```sh
PYTHONPYCACHEPREFIX=/private/tmp/p7-pycache PYTHONDONTWRITEBYTECODE=1 /Users/syang/miniconda3/bin/python3 -B -m py_compile .agent/tools/knowledge.py .agent/tools/knowledge_core/__init__.py .agent/tools/knowledge_core/approval.py .agent/tools/knowledge_core/bootstrap.py .agent/tools/knowledge_core/capture.py .agent/tools/knowledge_core/chunks.py .agent/tools/knowledge_core/config.py .agent/tools/knowledge_core/context.py .agent/tools/knowledge_core/drafts.py .agent/tools/knowledge_core/id_manager.py .agent/tools/knowledge_core/index.py .agent/tools/knowledge_core/parser.py .agent/tools/knowledge_core/provenance.py .agent/tools/knowledge_core/retrieval.py .agent/tools/knowledge_core/scope.py .agent/tools/knowledge_core/templates.py
```

Result: **exit 0; no output**. Compilation bytecode was directed outside the worktree.

## Limitations

- This evaluates only the fixed synthetic corpus and does not claim anything about real personal data, provider behavior, live daemon/plugin behavior, credentials, network conditions, or production relevance.
- The corpus has 12 notes and the manually fixed oracle has 20 queries; it is not a distributional benchmark.
- The location metric checks returned provenance integrity and line bounds, not semantic relevance or precision. Q02 returns two hits from the same source because the public interface returns snippets; the denominator is returned hits, not unique sources.
- No latency, incremental refresh, source mutation, grant lifecycle, or failure-recovery behavior was measured in this P7.2 run.
