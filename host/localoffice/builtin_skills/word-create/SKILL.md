---
name: word-create
description: Create an originally designed DOCX working copy and extend it in bounded sections using LocalOffice tools.
allowed-tools: word.plan_draft word.submit_draft word.select_source_quotes word.plan_content word.plan_requirements word.repair_content_plan word.refresh_fields word.load_tools word.create word.prepare_creation word.revise_creation word.edit_creation_blocks word.edit_creation_styles word.finalize_creation word.list_objects word.read_objects word.stage_batch ask_user finish
---

# Word creation

Create one working copy using the loaded contract, named styles and bounded blocks. Preserve supplied literals, requested design and citations; draft only when requested. Ask essential gaps; otherwise label placeholders.

Deliver the full scope. Extend long documents with insert_paragraphs and current anchors; never recreate an existing artifact or shorten the request. Repair structure without changing content. Respect annotation scope and review stops.

Use native page fields. PDF rendering does not update DOCX field caches; call refresh_fields separately only when requested. Check staged content/order/design against the request with bounded reads. Receipts prove execution, not correctness. Never replay successful edits. Finish after all requested work is staged; only the user Apply/Saves. Visual approval requires rendered inspection.

Planning creates no document. Repair with replace/defer_as_draft; defer only requested drafting. Never drop copied text or guess replacements.

Select quotes by q01..q24 and role, excluding formatting instructions; create with ct_ references. Load plan_content for unquoted/other-message sources; prepare_creation or create for drafting without quotes.

For retained corrections load edit_creation_blocks; load revise_creation for other patches or prepare_creation to replace the proposal. proposal_changed=false means no progress; compare against the request before another correction.
