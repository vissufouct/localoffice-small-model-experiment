# What other developers can reuse

This is MIT-licensed research source, not a stable SDK. Reuse the contracts and their tests together. A deterministic module can be valuable even though the whole agent did not achieve reliable Word-task completion.

## Components and extraction boundaries

| Component | Start here | Preserve when adapting | Qualification limit |
|---|---|---|---|
| Native tool protocol and constrained decoding | [native_tools.py](../host/localoffice/native_tools.py), [native_history.py](../host/localoffice/native_history.py), [decoding.py](../host/localoffice/decoding.py) | Exact call/result pairing, real compiled-grammar checks, argument validation and a legal clarification/exit path. Test the new model's actual tokenizer/template/parser. | Current evidence is model/backend-specific; valid syntax is not intent accuracy. |
| Transactional artifact staging | [storage.py](../host/localoffice/storage.py), [transaction tests](../tests/test_storage_transactions.py) | Current base revision, immutable candidate bytes, explicit Save, recoverable Undo, idempotency and stale-edit rejection. | Not a general filesystem sandbox or security certification. |
| Deterministic Word worker | [Open XML project](../workers/openxml/LocalOffice.OpenXml.csproj), [Word plugin](../host/localoffice/word/plugin.py) | Registered operation contracts, bounded inputs, worker validation, preservation and render checks after edits. | Worker support does not certify arbitrary imported documents or Microsoft Word pixel equivalence. |
| State-bound argument constraints | [page projection](../host/localoffice/word/page_requirement_projection.py), [style inventory](../host/localoffice/word/style_inventory.py), [native style projection tests](../tests/test_creation_style_projection.py) | Project only values selected from authoritative current state; invalidate stale inventories; do not infer missing user requirements. | Can constrain execution of a selected interpretation, not prove the interpretation was right. |
| Draft provenance and shared budgets | [draft fragments](../host/localoffice/word/draft_fragments.py), [draft budget](../host/localoffice/draft_budget.py), [placement verifier tests](../tests/test_b1_focused_placement.py) | Distinguish exact literals, generated prose, current draft generation and final placement. Count retries/substeps against the same task budget. | Separate drafting did not establish reliable end-to-end results. |
| Durable memory and bounded graph retrieval | [memory.py](../host/localoffice/memory.py), [graph.py](../host/localoffice/graph.py), [memory tests](../tests/test_memory.py), [graph tests](../tests/test_graph.py) | Ledger/file hashes remain authoritative; summaries are navigation aids; preserve chat scope, decisions and recovery semantics. | No controlled evidence that the graph improved model accuracy. |
| Lazy skills and extension contracts | [skills.py](../host/localoffice/skills.py), [built-in creation skill](../host/localoffice/builtin_skills/word-create/SKILL.md), [contracts](../packages/contracts/index.ts) | Pin skill content/version, account for its tokens, validate required tools and refuse incompatible imports explicitly. | Importing a SKILL.md does not make every Claude/Codex skill executable or compatible. |
| Reviewable desktop workflow | [desktop source](../apps/desktop/src/main.ts), [host](../host/localoffice/server.py) | Separate chat context from shared project files; expose staged changes, pause/annotation and explicit Apply/Save. | UI availability and successful project creation do not prove model task accuracy. |
| Evaluation methodology | [B1 benchmark](../scripts/benchmark_word_body.py), [fixture](benchmarks/word-body-iteration-fixtures.md), [accuracy findings](ACCURACY_EXPERIMENTS.md) | Freeze prompts/budgets; preserve failures; separate mechanical, semantic, visual and lifecycle checks; measure whole-task cost. | Tiny exploratory cohorts are not a benchmark leaderboard. |

## A practical reuse sequence

1. **Start without a model.** Feed explicit operations into the storage/worker layer using disposable files. Test preservation, rejected inputs, stale revisions, idempotency, Save and Undo.
2. **Add one model adapter.** Verify the actual native template/parser and compiled grammar on valid and invalid payloads before measuring task accuracy. The grammar regression tests deliberately retain known limitations.
3. **Define a frozen complete task.** Include the desired meaning and artifact placement, not only word counts or valid DOCX structure. Predeclare context, step, time and memory limits.
4. **Add context reduction one mechanism at a time.** Retain the authoritative objective and decisions; record exactly which optional information is removed. A tokenizer-only admission pass is not an inference pass.
5. **Measure complete outcomes.** Report calls, retries, total tokens, latency and memory alongside task success. View actual rendered outputs and record revision/reviewer. Retain all failed runs.
6. **Change models or prompts in new cohorts.** Keep the original results. Compare consistent task requirements; do not quietly soften them when a candidate looks good.

## Useful checks to start with

After installing the documented Python/MLX development dependencies:

```sh
.venv/bin/python -m pytest -q \
  tests/test_storage_transactions.py \
  tests/test_creation_revision_value_grammar.py \
  tests/test_creation_style_projection.py \
  tests/test_draft_budget.py \
  tests/test_b1_focused_placement.py
```

This is a suggested contract-test selection, not a claim that this entire selection was freshly executed during the documentation update. See [VALIDATION.md](VALIDATION.md) for the checks actually run. Word integration tests and real-model benchmarks require additional dependencies described in [REPRODUCE.md](REPRODUCE.md).

## What not to copy blindly

- Do not ship experimental prompt/tool-routing choices as a “reliable 4B configuration.” They remain unqualified.
- Do not assume the complete request can safely be replaced with a model-selected excerpt. Conversely, adding it back did not uniformly improve our diagnostic.
- Do not force a finish or invent automatic repairs to make a benchmark pass. Preserve remaining-edit, export, review and validation requirements.
- Do not redistribute private project databases or model paths to reproduce a saved-state experiment. Build a fresh synthetic fixture instead.
- Do not equate a large passing deterministic suite with broad model reliability, rendering fidelity or a 16 GB resource guarantee.

## Suggested next research—not demonstrated solutions

A developer can use this package to compare compatible models, test alternative task decomposition, evaluate constraint-preserving retrieval, or design explicit completion verification. Use held-out tasks and repeated unchanged runs to avoid fitting prompts to B1. The experiment identifies failure modes and reusable controls; it does not establish which future intervention will solve them.
