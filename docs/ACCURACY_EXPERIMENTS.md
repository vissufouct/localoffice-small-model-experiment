# Improving small-model accuracy: what we tried and what happened

## Conclusion

We improved the harness's ability to constrain, validate, execute and audit model-selected actions. We did **not** establish reliable complete-task Word creation with the pinned 4B model. Some intermediate documents became correct while the agent still failed to preserve its own draft or finish within its budget. These are different outcomes.

This report distinguishes **implemented safeguards**, **partial observed improvements**, **unsuccessful experiments**, and **unmeasured hypotheses**. “Worked” below always names the level at which it worked; it never means general small-model accuracy was solved.

## The problem we were testing

The model had to interpret a request, select registered tools, supply exact arguments, inspect results, repair problems and stop when finished. Deterministic workers—not model-written scripts—performed file operations. We tried to move mechanical complexity out of the model while retaining its responsibility for meaning, design choices and tool selection.

The final B1 fixture was deliberately small but combined exact copying with new prose: three paragraphs, two supplied Unicode strings, a neutral English invitation of 8–16 words, explicit typography/page geometry and a page break. Correct output also required staging, finishing, preserved draft placement and lifecycle verification. The [frozen fixture](benchmarks/word-body-iteration-fixtures.md) and [experiment configuration](EXPERIMENT.md) define the full requirements.

The final model was Qwen3-4B-Instruct-2507, MLX 4-bit, at the pinned revision recorded in EXPERIMENT.md. We did not silently switch models or increase budgets to rescue failures. Earlier harness/backend demonstrations are not evidence that this Word configuration succeeded.

## Accuracy interventions

| Intervention and intended mechanism | What worked or was observed | What failed / remains unproven | Disposition |
|---|---|---|---|
| **Native tool templates and role/call-ID preservation.** Use the model's native Qwen protocol and exact tool-result pairing instead of arbitrary prompt wrappers. | Calls could be parsed, reconciled and attributed to exact executions; protocol behaviour has deterministic tests. | Correct protocol did not ensure correct tool selection, prose or stopping. No controlled comparison proves an accuracy uplift over another template. | Retained infrastructure. |
| **Constrained decoding and schema validation.** Limit available tool names and argument shapes, with a legal clarification/exit path. | Rejected invalid payloads before dispatch; grammar tests check actual tool envelopes. | Grammatically valid calls could still copy the wrong text, target the wrong object or make no progress. Compiler defects also caused failures initially. | Retained, with known grammar limits. |
| **Fix the grammar compiler before blaming the model.** An unrestricted JSON revision value was composed incorrectly inside a tool envelope. | Grouping the schema restored acceptance of strings, numbers, objects, arrays and null as well as booleans. Regression tests exercise the compiled regex. | This repaired the interface, not the model's intent. Existing backend nesting/Unicode-escape limitations remain explicitly tested. | Concrete harness bug fix. |
| **Compact tools and lazily loaded skills.** Offer task-relevant operations and small receipts; preserve the full request while removing redundant context. | Tokenizer admission probes fit narrowly within the 3200-token input allowance; operations remained deterministic. | Fitting context did not imply understanding. Repeated calls increased total task cost; no general efficiency advantage was established. | Retained budget control, not a proven semantic router. |
| **Source-bound literal planning and separate generated-text references.** Distinguish supplied quotations from prose the model must create. | Exact references and immutable draft records made substitutions traceable. | Broad selections included neighbouring literal instructions; the model sometimes submitted the first literal instead of writing the invitation. | Useful provenance; semantic selection unresolved. |
| **Model-selected requirement checks and page-value projection.** Validate selected requirements and carry explicitly selected page geometry into creation arguments. | Subsequent runs used correct one-inch margins; deterministic checks caught geometry, font and word-count mismatches. | The model could omit/misinterpret requirements or fail to repair them. Checking a selected subset is not checking the complete request. No isolated causal proof for the run-to-run change. | Retained with selection/coverage limitations. |
| **Specific repair errors and no-progress feedback.** Explain absent style entries, invalid paths and unchanged revisions. | Invalid changes were rejected and no-op repairs became observable. | The model repeated ineffective revisions, used the wrong style paths or asked the user to reconfirm supplied instructions. | Retained diagnostics; insufficient self-correction. |
| **Typed style declaration/update plus state-bound enums.** Replace fragile generic style paths with explicit operations; restrict update IDs to verified existing styles. | Later runs declared the required named style and produced correctly formatted candidates. | Earlier typed calls tried to declare and update the same style together and were rejected. Later whole tasks still failed. | Reusable operation design; no full-task reliability claim. |
| **Separate drafting from document assembly.** Give drafting its own small context and tools, then return its exact text to the parent. | Some runs produced valid invitations and preserved them in the proposal; shared durable step budgets prevented hidden extra attempts. | Other runs copied literals, substituted different text during assembly or exhausted the budget. Isolated v1: 0/6 accepted; stricter v2: 0/1. | Implemented experimental mode, not a qualified app default. |
| **Post-creation context recycling and lifecycle-aware tool availability.** Unload optional inspection tools only with fresh execution evidence; stop automatically offering creation drafting after a document exists. | Context fallback worked in an observed run; later runs produced correct two-page candidates without automatic re-exposure of the draft tool. | The model still loaded unnecessary tools, searched existing text and exhausted its ten-step allowance. | Retained lifecycle fix; stopping accuracy unresolved. |
| **Single-segment versus range selection.** Let the model select one source segment directly instead of always specifying a range. | Exact selection changed from 1/3 to 2/3 in the small paired probe. | The alternative omitted more distributed constraint segments. Better aggregate exact-span count concealed a worse failure mode. | Not integrated as a fix. |
| **Completion-review context and reduced tool set.** Present current body evidence and finish/clarify/load options to encourage a justified stop. | Three admitted payloads ran within budget; an oversized variant was excluded explicitly. | Baseline requested another read; both review variants asked to insert text already present. No finish selection. | Not integrated as a fix. |
| **Selected focus plus complete objective.** Restore constraints that a narrow excerpt may omit. | The operator-selected Portuguese counterfactual changed from failure to a valid five-word Portuguese sentence. | Observed cases fell from 2/3 baseline semantic passes to 1/3. Both variants copied the B1 literal; augmentation also caused an unnecessary clarification. | Mixed evidence; not integrated as a fix. |

The source includes these mechanisms, but not every experimental mode is enabled by default. See the explicit benchmark flags in [REPRODUCE.md](REPRODUCE.md). Avoid interpreting code presence as release acceptance.

## A concrete failure that explains the distinction

In the [latest strict B1 run](evidence/word-body-b1-isolated-plan-v2-attempt1/report.json), the model selected a broad drafting excerpt and submitted `Síntese — café e inovação.`—the first supplied literal—as its draft. Document preparation separately produced a valid invitation and eventually a correct two-page candidate. The model then requested unnecessary capabilities and searched text until it exhausted ten steps.

This run failed for two independent reasons: it did not complete within the task budget, and its submitted draft was not the second paragraph. A screenshot or document-structure check alone would miss the second problem. The stricter verifier checks the current draft generation against the actual target paragraph. The [visual review record](evidence/word-body-b1-isolated-plan-v2-attempt1/visual-review.json) documents what was actually viewed; review records do not replace the omitted artifact images in this public export.

The run took about 121.85 seconds, with 10 model calls, 10 tool calls and one repair; recorded usage was 25,997 input and 1,120 output tokens. A compact per-call context did not guarantee a cheap or successful complete task.

## What developers can reasonably take away

**Worked at the engineering level:** deterministic document mutation, schema/argument rejection, revision-bound staging, exact execution provenance, bounded context admission and stronger acceptance checks. These help prevent or expose errors and are reusable even with a different model.

**Worked only partially in model runs:** correct page geometry and named styles, some valid drafted sentences, and correct rendered intermediate documents. These observations identify promising components; they do not establish consistent accuracy or isolate which change caused improvement.

**Failed at the goal level:** consistent interpretation of mixed literal/generative instructions, keeping distributed constraints, reliable self-repair, preserving generated text through assembly, and stopping appropriately. Complete-task acceptance remained open.

**Not demonstrated:** a universally optimal 3–5-tool count; accuracy gains from embeddings-based routing; gains attributable to memory/knowledge graphs; a benefit from exposing model reasoning; broad model-family compatibility; physical 16 GB usability; general superiority over another harness; impossibility of solving these tasks with small models. Those were ideas, architectural motivations or future work, not established findings.

## Evidence and reproducibility limits

- [Scope-interface review](evidence/draft-scope-interface-v1-attempt1-review.json): three paired cases; no production integration.
- [Completion review](evidence/completion-review-selection-v1-attempt1-review.json): terminal-state selection only, no actions dispatched and no task acceptance credit.
- [Full-objective raw responses](evidence/draft-focus-objective-v1-attempt1.json) and [semantic review](evidence/draft-focus-objective-v1-attempt1-review.json): eight calls, three observed selections plus one clearly labelled operator counterfactual. The counterfactual does not measure model scope selection.
- [Latest full-task report](evidence/word-body-b1-isolated-plan-v2-attempt1/report.json): failed strict v2 task, separate from earlier v1 outcomes.
- Source/tests demonstrate implementation contracts. Historical checkpoint narratives support the intermediate observations above; not every earlier run is redistributed. Do not treat an omitted raw report as publicly replayable evidence.

These were exploratory iterations, not a preregistered statistically powered study. Cohorts used different harness/verifier versions; do not pool them or turn descriptive counts into a general model accuracy percentage. Public JSON is redacted and wrapped under `data`; historical hashes refer to originals. Private saved-state probes are not replayable from the export. A fresh full B1 run is reproducible through the documented setup, subject to dependencies and hardware; stochastic outputs need not match.

For practical extraction steps and tests, see [REUSE.md](REUSE.md).
