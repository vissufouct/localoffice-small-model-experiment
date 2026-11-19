# Experiment record

For a technique-by-technique account of accuracy work, read [ACCURACY_EXPERIMENTS.md](ACCURACY_EXPERIMENTS.md). For extraction paths and contracts, read [REUSE.md](REUSE.md).

## Objective and scope

Build a plugin-first, local-only-by-default Office harness with a project/chat sidebar, live or background previews, pause/selection annotations, working copies, explicit Save and Undo. Use compact registered tools and lazily loaded skills instead of asking the model to regenerate whole documents or write arbitrary scripts/XML for routine edits. Add durable memory and bounded graph navigation to support continuity with narrow context.

Planned order: harness UI, Word, PowerPoint, Excel, later image generation. Existing-image placement belonged in Word and PowerPoint. Professional original design was required; macros and advanced collaboration were outside the baseline. Development stopped during Word stabilization.

## Experimental configuration

Pinned Word model: mlx-community/Qwen3-4B-Instruct-2507-4bit, revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b`. Native Qwen tool formatting, experimental constrained decoding, temperature 0.2, top_p 1, top_k 0, min_p 0. Context 4096 tokens, output reserve 768, safety reserve 128: input allowance 3200. Model and workers execute locally. Weights must be obtained separately under their own licence.

B1 is a frozen body-creation fixture: three paragraphs, two exact Unicode literals, one newly drafted neutral English invitation of 8–16 words, named Arial 12 style, A4 portrait, one-inch margins, break before paragraph three, no extras, stage and finish. Limits: ten responses and 150 seconds. Full acceptance additionally requires exact draft placement, semantic and rendered visual review, and the Save/Undo/restore lifecycle. Read the frozen fixture; do not turn a stopped task or attractive candidate into a pass.

## Evidence at shutdown

| Evidence | Result | What it does not establish |
|---|---|---|
| Historical full source regression run | 2799 passed, 383 seconds | Real-model intent accuracy, native UI, Microsoft Word compatibility or 16 GB qualification |
| Isolated drafting v1 complete-task cohort | 0/6 | General statistical failure rate; harness changed during exploration |
| Stricter draft-placement v2 cohort | 0/1 | Long-run reliability estimate |
| Latest v2 candidate | Correct two-page candidate, inspected visually; task exhausted ten steps | Successful complete-task lifecycle or correct submitted-draft handoff |
| Scope interface probe | Exact spans 1/3 versus 2/3, but alternative omitted more distributed constraints | A justified production improvement |
| Completion context probe | Read / unnecessary clarification / unnecessary clarification | Improved completion |
| Full-objective drafting probe | Observed cases 2/3 baseline versus 1/3 augmented; Portuguese counterfactual 0/1 versus 1/1 | General improvement or scope-selection accuracy |

These are small, exploratory, mostly non-independent samples. Do not pool cohorts, infer confidence intervals or claim causality. Runtime changes and verifier versions matter. The 2799-test result is historical source-workspace evidence; it is not a fresh clean-machine reproduction of this export.

## Lessons supported by observations

1. Valid tool syntax is not semantic correctness. Grammar and validation prevented malformed calls but did not make the model choose the right text or finish at the right time.
2. Reducing context can discard necessary distributed constraints. Adding the whole request back can also increase confusion. Neither extreme was a universal fix.
3. Exact execution receipts, revision checks and atomic staging remain useful even when model behaviour is poor.
4. Intermediate artifact quality and end-to-end agent acceptance must be measured separately.
5. Full objective, selected source span, generated prose and final placement need distinct provenance. The stricter verifier exposed mismatches that candidate-only checks missed.
6. Fewer tokens per call do not necessarily mean lower complete-task cost when retries and unnecessary calls increase.

## Reuse and future experiments

Extract deterministic Word operations, transactional artifact storage, bounded context assembly and tool validation independently. Keep fixtures and failure evidence before changing models or prompts. Compare compatible models in separately labelled cohorts, predeclare budgets, preserve all failures, and inspect actual rendered outputs. No claim is made that these results generalise to every 4B model or that larger models are required.

Curated JSON evidence is redacted for portability/privacy and is not a byte-identical archival copy. Original source hashes inside reports are historical identifiers, not hashes of redacted files. Saved-state probes requiring private temporary databases cannot be replayed from this public package; fresh B1 tasks can be run from source.
