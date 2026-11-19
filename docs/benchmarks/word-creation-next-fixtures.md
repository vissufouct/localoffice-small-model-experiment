# Next Word creation fixtures — frozen diagnostic specification v1

Frozen before inference: 2026-11-17. This file defines two new diagnostic fixtures; it records no execution or acceptance result. The original two-page fixture and its assertions remain unchanged. These cases extend coverage; they do not replace the full professional Word scope.

## Common execution contract

- Use the existing `scripts/demo_word_page_bands.py` native task/report lifecycle as the harness basis, with the exact prompts below. The current script does not yet select these fixtures; a later implementation must identify the fixture ID/version explicitly in its report.
- Pin MLX `mlx-community/Qwen3-4B-Instruct-2507-4bit`, revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b`. Native constrained decoding, source planning and opt-in creation review remain enabled. Use the unchanged low-temperature profile: temperature 0.2, top_p 1.0, top_k 0, min_p 0.0.
- Context remains 4096 tokens, with 768 output tokens and 128 safety tokens reserved. No model/backend/profile substitution, extra prompt hints, scripted-prefix injection or operator repair during a trial.
- Start each attempt in a fresh project with absent `report.docx`, no candidate, historical references or retained proposals. Use live mode, preserving the automatic render boundary and revision-bound current-render facts.
- Diagnostic step limits count every inference response, including invalid responses, tool loading, proposal preparation/revision/finalization, reads, questions and finish. Each response permits at most one registered native call. Limits below are not additional editing allowances. A timeout, clarification stop, context admission failure or step-limit termination is a failed complete-task attempt even if its candidate is correct.
- Require terminal `completed`, no terminal error, no unnecessary question, and the entire artifact assertions below. Recoverable errors remain in the report; success does not erase retries.
- Require exactly one successful document-staging operation, producing one new DOCX. Proposal preparation/revision and source planning are metadata operations and do not count as document staging. This is the same transaction assertion as the basic fixture, not a general ban on multi-operation professional tasks.
- Before user-side application, the target must remain absent on disk. Verify the final candidate's package integrity and XML assertions, and its revision-bound PDF. Save the candidate, record its exact bytes, Undo creation to absence, then Restore to byte-identical saved bytes. Application, Undo and Restore are harness verification after model completion, not model permissions to Apply/Save.
- A native PAGE/NUMPAGES cache of `1` in the DOCX is not a pagination failure when the field instructions and rendered values are correct. Neither prompt requests saved-cache refresh. PDF page count alone does not prove correct numbering, literal content or visual composition.
- Evaluate all final rendered pages. Record candidate revision, PDF hash, page images actually viewed, reviewer identity and findings. Text extraction and XML checks are separate evidence; neither establishes visual review.
- Preserve all attempts, prompts, native calls/results, errors, selected skill hash, configuration and final statuses. Record complete-task input/output tokens, model/tool calls, retries, payload sizes, context recycling/admission failures, latency and peak memory. Mark unknown/incomplete measurements explicitly.

### Repetition and budget interpretation

Run one diagnostic attempt per fixture first. After a pass, obtain three unchanged independent passing repetitions per fixture, counting the initial pass as the first. Any intervening failure breaks the consecutive passing sequence and remains reported; no selective deletion. A change to prompt, assertions, runtime, model settings or budgets starts a new version/cohort and must be identified.

The step limits and watchdogs below are frozen diagnostic termination limits. Observed tokens, latency, memory and calls are informational measurements, not frozen release-efficiency acceptance thresholds. Final release-efficiency budgets, broader short/long/reference/batching comparisons and hardware qualification must be specified separately before that evaluation. Passing deterministic tests or a scripted fixture does not count as a real-model repetition.

## Fixture C1 — exact Unicode literals, requested drafting and distinct typography

**Diagnostic limit:** 10 inference steps, matching the original baseline; at most 10 native-call attempts. **Watchdog:** 150 seconds for the model task, matching the baseline. No extension during an attempt.

### Exact prompt

```text
Create report.docx on A4 portrait with one-inch margins on all four sides. Use Arial 12 point for every body paragraph, Arial 10 point for the header and Arial 9 point for the footer, using named styles. The header must be right-aligned and contain exactly "Relatório de revisão". The footer must be centered and show the literal prefix "Página " followed by the current page number, the literal separator " de ", and the total page count, using real page-number fields. Spaces inside those quoted prefix and separator strings are intentional; do not print the quotation marks. Create exactly three body paragraphs in this order: first, exactly "Síntese — café e inovação."; second, draft one neutral English sentence of 8–16 words inviting the reader to review this document, without claiming a review has already happened or adding factual claims about an organisation; third, exactly "Conclusão — próximos passos.". Put a page break before the third paragraph. Do not add a title, other body text, tables or images. Stage the document and finish.
```

### Frozen artifact assertions

1. Exactly three direct body paragraphs, in the stated order. Paragraphs one and three equal the quoted literals codepoint-for-codepoint, excluding quotation marks. Do not normalize away accents, punctuation or spaces. No extra empty/page-break paragraph, title, table, drawing or content-control body block.
2. Paragraph two is model-drafted plain text, not a source handle, instruction echo or a forced golden sentence. Count words by splitting on Unicode whitespace: 8–16 tokens. Require one English invitation sentence and no assertion that review already occurred, invented organisation facts or extra requests. Semantic judgement is recorded explicitly by the reviewer; word count alone is insufficient. Do not require an explicit source ref for this drafting slot.
3. Paragraph three has an enabled paragraph-level `pageBreakBefore`; paragraphs one and two have no enabled page-break-before or explicit page break. An accepted standalone page-break proposal must normalize to this same final structure.
4. All three body paragraphs reference declared paragraph styles whose font is Arial and size is 24 half-points. Header and footer paragraphs reference declared styles with Arial and sizes 20 and 18 half-points respectively. Style IDs are implementation choices, not prescribed names. No conflicting direct run font/size override may defeat these values.
5. Exactly one explicit default header and one explicit default footer, each containing one paragraph; no first/even story references or enabled first/even switches. Header text is exactly `Relatório de revisão`, right-aligned. Footer is centered and contains the ordered native sequence: literal `Página `, PAGE field, literal ` de `, NUMPAGES field. Field instruction whitespace may vary; field kinds and literal characters may not. No hard-coded page/total text in place of fields.
6. Section page size is 11906 × 16838 twips, portrait; top/right/bottom/left margins are 1440 twips. Reject contradictory orientation or page geometry.
7. Final rendered PDF has exactly two pages. Page one contains the first literal and the draft, with no third literal. Page two contains the third literal, with no first literal or draft repeated in the body. Both pages contain the exact header; footers render `Página 1 de 2` and `Página 2 de 2` on their respective pages. PDF extraction may collapse layout whitespace; exact literal spacing is asserted in XML separately.
8. Apply all common lifecycle, completion, one-staging-operation and exact Undo/Restore assertions. Unused declared styles are not a failure unless they affect the artifact or violate another bound.

### Visual criteria

View both pages of the final revision. Confirm three readable body paragraphs in the intended page distribution, no clipping or accidental blank page, right-aligned header and centered footer within the margins, and no body/band overlap. Confirm sensible line breaks in the drafted sentence and intact accented characters/em dashes. Typography measurements come from XML; visual inspection checks legibility and composition rather than estimating point sizes from pixels. Do not impose unrequested colours, decorative elements or a specific draft sentence.

**Reuse:** baseline live native harness and Save/Undo/Restore assertions; source-binding examples in `tests/test_word_literal_creation.py`; typography contracts in `host/localoffice/word/contracts.py`. This is a new real-model prompt variant, not a claim that the existing test already qualifies it.

## Fixture C2 — four-page first/even/default native bands

**Diagnostic limit:** 12 inference steps; at most 12 native-call attempts. **Watchdog:** 180 seconds for the model task. These limits are frozen before the first inference. The two additional steps relative to C1 accommodate inspection of two additional band categories and their page attribution, while retaining a bounded completion requirement. They do not mandate extra reads or allow retries beyond the existing runtime rules. Rendering four pages does not itself grant additional model calls.

### Exact prompt

```text
Create report.docx on A4 portrait. Use Arial 11 point for the body and Arial 9 point for every header and footer, using named styles. Create exactly four body paragraphs in order: "Body page 1", "Body page 2", "Body page 3", and "Body page 4". Put a page break before each paragraph after the first so the document has four pages. Use a different first-page header and footer and different even-page headers and footers. On the first page, use the right-aligned header "First header" and the centered footer prefix "First page ". On even pages, use the right-aligned header "Even header" and the centered footer prefix "Even page ". For the default odd-page header and footer, use the right-aligned header "Default header" and the centered footer prefix "Default page ". After each footer prefix, insert the current page number, the literal separator " of ", and the total page count, using real page-number fields. Spaces inside the quoted prefixes and separator are intentional; do not print the quotation marks. Do not add other body text, tables or images. Stage the document and finish.
```

### Frozen artifact assertions

1. Exactly four direct body paragraphs, with exact respective text `Body page 1`, `Body page 2`, `Body page 3`, `Body page 4`. Paragraphs two through four have enabled paragraph-level page-break-before; paragraph one does not. No extra empty/break paragraphs, explicit redundant page breaks, body tables, drawings or content controls.
2. One final section, with enabled `titlePg` and document `evenAndOddHeaders`. Exactly one explicit reference for each of default/first/even header and footer, resolving internally to six distinct valid story parts. Resolve relationships; do not assume filenames or relationship IDs. Each story has exactly one paragraph.
3. Header texts and right alignment are exact: `First header`, `Even header`, `Default header` in their corresponding story types. Each centered footer has its corresponding exact literal prefix (`First page `, `Even page ` or `Default page `), native PAGE field, exact separator ` of `, then native NUMPAGES field. No literal totals substituted for fields, extra wording or unintended story content.
4. Every body paragraph references a declared Arial 22-half-point style; every band paragraph references a declared Arial 18-half-point style. Shared styles are allowed. Style IDs and declaration ordering are not prescribed. No conflicting direct run font/size override.
5. Page size is 11906 × 16838 twips, portrait. Margins and band distances are intentionally unspecified in this prompt; do not add a numeric assertion for them. The resulting layout must still satisfy the visual criteria.
6. Final PDF has exactly four pages. Required page-local content is:

   | Page | Body | Header | Footer |
   |---|---|---|---|
   | 1 | Body page 1 | First header | First page 1 of 4 |
   | 2 | Body page 2 | Even header | Even page 2 of 4 |
   | 3 | Body page 3 | Default header | Default page 3 of 4 |
   | 4 | Body page 4 | Even header | Even page 4 of 4 |

   Each page must exclude the other header labels, footer-category prefixes and body-page labels. Compare body and footer strings case-sensitively. As in C1, XML checks preserve exact literal spacing; PDF extraction may collapse layout whitespace.
7. Apply all common lifecycle, completion, one-staging-operation and exact Undo/Restore assertions. The two even pages must share the intended even story and evaluate PAGE differently on each rendered page; a shared DOCX cache is not evidence of two identical rendered numbers.

### Visual criteria

View all four pages of the final revision. Confirm first/even/default/even band attribution, visible right-aligned headers and centered footers, continuous numbering 1–4 with total 4, readable Arial text, and one correctly located body paragraph per page. No fifth/blank page, overlapping bands, clipping, displaced body text or leaked alternate header/footer wording. Do not claim inherited-section behaviour: this fixture deliberately contains one section and explicit stories.

**Reuse:** the content and per-page expectations of `scripts/render_word_band_variants.py`, plus structural assertions from `tests/test_word_band_variants.py`, through the same native model lifecycle as C1. The existing four-page deterministic render is supporting fixture evidence, not a real-model result.

## Scope retained after these fixtures

Even three passing repetitions of both cases would establish only these bounded creation behaviours. Existing-document variant editing/section linking, asymmetric variants, tables, floating-image composition and preservation, remaining numbering operations, long-document continuation, imported-file preservation, original-design breadth, native UI, Microsoft Word and physical 16 GB execution remain separate acceptance work. No fixture here narrows or marks those requirements complete.
