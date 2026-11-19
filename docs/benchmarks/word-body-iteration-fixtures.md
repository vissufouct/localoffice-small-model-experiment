# Word body iteration — frozen diagnostic fixture B1 v1

Scope decision: the user deferred headers and footers to the next Word iteration. B1 is a new body-only diagnostic, not a reinterpretation or replacement of historical C1/C2 results. Passing B1 does not establish the full body iteration, professional baseline, or full Phase 2 acceptance. Precise editing, preservation, tables, existing-image placement, PDF handling, native UI and physical 16 GB qualification remain separate requirements.

## B1 — exact text, requested drafting and pagination

Freeze this prompt and all assertions before the first inference attempt. Changes require a new fixture version. Use a fresh project and an absent report.docx.

```text
Create report.docx on A4 portrait with one-inch margins. Use Arial 12 point for all body text, using a named paragraph style. Create exactly three body paragraphs in this order: first, exactly "Síntese — café e inovação."; second, draft one neutral English sentence of 8–16 words inviting the reader to review the document, without claiming a review has already happened or adding factual claims about an organisation; third, exactly "Conclusão — próximos passos.". Put a page break before the third paragraph. Do not add a title, other body text, headers, footers, page numbers, tables or images. Stage the document and finish.
```

## Execution contract

- Pinned local Qwen3-4B-Instruct-2507 MLX 4-bit revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b`; native constrained decoding and reviewed creation. No substitute model, cloud, operator corrections or hidden prompt additions.
- Context 4096; output reserve 768; safety reserve 128. Temperature 0.2, top_p 1.0, top_k 0, min_p 0.0.
- At most 10 inference responses and 150 seconds for the task. Retain every retry, clarification, truncation, failed proposal and context-capacity failure. No allowance increases after seeing results.
- Live preview mode, one successful staging operation, terminal completed without terminal error or unnecessary clarification. Record complete-task tokens, calls, retries, payload sizes, latency, memory and runtime fingerprint. Any source changes during inference invalidate stable-cohort qualification.

## Artifact assertions

1. Exactly three direct body paragraphs, in the requested order. First and third match the supplied Unicode literals codepoint-for-codepoint. No extra empty paragraph, title, explicit break-only paragraph, table, drawing or content control.
2. Paragraph two is newly drafted plain text: one English invitation sentence, 8–16 whitespace-separated words, with no claim that review occurred and no invented organisational facts. Record an explicit semantic review; word count is not sufficient.
3. Paragraph three has paragraph-level pageBreakBefore enabled. Neither earlier paragraph has a page break. A standalone break in the model proposal may normalize to this same structure.
4. All body paragraphs reference declared paragraph styles with Arial and size 24 half-points. Reject conflicting direct or theme-font overrides. Style IDs are not prescribed.
5. A4 portrait is 11906 × 16838 twips; all margins 1440 twips. No header/footer story references, active first/even switches, page-number fields or other unrequested story content.
6. Rendered PDF has exactly two pages. First literal and drafted sentence occur on page one; third literal on page two. No duplicated body text or unintended blank page.
7. Valid OOXML, one staged working copy, no automatic save. Applying saves identical candidate bytes. Undo restores target absence; restore returns byte-identical DOCX. PDF export and rendered revision must be tied to the exact candidate hash.

## Review and stability

Inspect both rendered pages of the final revision. Record image paths, revision, PDF hash, reviewer and viewed page numbers. Confirm readable text, accents, line breaks, margins and no clipping. Do not claim model vision from XML or PDF text extraction.

A machine pass remains pending visual and semantic review. Require three unchanged consecutive complete-task passes for this fixture's narrow reliability claim. Keep failed attempts and original C1/C2 histories. No B1 runs or successes are recorded by this specification.

## Explicit requirements-planning comparison

The optional `--requirements-planning` diagnostic enables the experimental planning prerequisite and records `requirements_planning: true` with cohort `requirements_reviewed_v1`. It retains the exact prompt, pinned model, decoding mode, sampling, 4096/768/128 context configuration, ten-response/150-second limits and every artifact assertion above. The model must select its own requirements; no scripted declarations or corrections enter real-model runs. Default runs retain `frozen_reviewed_v1`. Keep the cohorts distinguishable and do not reinterpret historical failures or combine unlike runtime versions into a stability streak. This option does not enable the experimental workflow in the desktop app.

## Focused drafting diagnostic cohort v1

Historical contract below; new focused/isolated runs use the strengthened v2 verifier described at the end. Historical evidence and cohort labels remain unchanged.

`--requirements-planning --focused-drafting` selects `focused_drafting_reviewed_v1`, separate from earlier cohorts. The B1 prompt/hash, pinned model, sampling, 4096/768/128 context reserves, ten total responses and 150-second watchdog remain unchanged. Draft planning/submission/clarification and retries share that response limit; resume does not reset it. All original artifact, lifecycle, semantic and visual assertions remain mandatory.

Additional integration assertion: the model must select the drafting instruction and submit a generated fragment; the retained fragment must appear exactly as a body paragraph in the final revision. Report its selected instruction, reference, generation and text, and the candidate revision. This verifies the tested handoff only, not semantic fidelity or correct source selection. If the model revises the fragment instead of using it exactly, preserve that evidence as a failed handoff diagnostic; do not infer acceptance from a valid file. General future multi-fragment/revision workflows are not restricted by this diagnostic assertion.

Failures without an artifact retain draft state and execution metrics; no visual review is claimed. No helper writes draft text into the document automatically. Three unchanged complete passes are still required for a narrow stability claim; this cohort does not qualify full Phase2.

## Isolated draft planning diagnostic cohort v1

`--requirements-planning --focused-drafting --isolated-draft-planning` selects the separate `isolated_draft_planning_reviewed_v1` cohort. All B1 and focused-drafting assertions remain unchanged, including exact submitted-fragment placement, semantic/visual review and the original model/sampling/context/10-response/150-second limits. Instruction selection runs as an existing main-loop inference step with a smaller planning context; it is not an extra uncounted model call. The complete objective catalogue and applicable user constraints remain available. The model can return to document tools through `word.load_tools` instead of inventing a draft or asking an unnecessary question. Resume must retain the exact persisted mode. Prior focused-drafting failures stay in their original cohort; no earlier result is reclassified.

## Focused handoff verifier v2 — new runs

New B1 focused and isolated runs use `focused_drafting_reviewed_v2` and `isolated_draft_planning_reviewed_v2`, respectively, with `focused_verifier_version: 2`. The underlying B1 user fixture remains v1: its complete prompt/hash, model, sampling, runtime limits and artifact/lifecycle assertions are unchanged. This versions the strengthened integration verifier, not the user objective or production drafting semantics.

B1 explicitly sets `expected_generated_body_index=1` (zero-based paragraph two). After the existing three-paragraph validation, the current active submitted generation must match that paragraph codepoint-for-codepoint. Matching only the copied first or third literal fails; a different valid invitation also fails the exact handoff. No historical/superseded generation may substitute for the current generation. Record expected index, all matching indices, reference, generation, text, instruction, candidate revision and verifier version, including on placement failure. Semantic and visual review remain mandatory; equality is not meaning approval.

Preserve all v1 attempts and failures without relabelling or retroactive acceptance. Start a separate unchanged-run streak for v2. This benchmark-only requirement does not constrain general multi-fragment/revision workflows or automatically place text.
