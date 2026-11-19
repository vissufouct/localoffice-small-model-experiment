---
name: word-edit
description: Make precise DOCX object and formatting changes using revision-bound LocalOffice tools.
allowed-tools: word.refresh_fields word.load_tools word.list_objects word.find_text word.read_objects word.stage_batch word.format_text format_text word.replace_text replace_text ask_user finish
---

# Word editing

Load only the requested operations with `word.load_tools`; their guidance supplies exact call shapes and operation-specific limits. Change the loaded set as needed, retaining the full task scope. Tool loading is not execution or permission.

Find quoted text with `word.find_text`; choose headers/footers scope for those stories. Follow pagination before concluding absence. Discover other targets with `word.list_objects`. Read exact IDs/hashes at the current revision; never guess locators. Use `replace_text` for words, `format_text` for emphasis, and native `set_list` for numbering. Shared header/footer edits affect all uses.

Stage sparse requested properties only. Preserve unrelated content, formatting and annotation scope; never broaden a selection. Batch compatible edits; structural insertion stands alone and invalidates positional locators. Read new locators afterwards. A PDF preview does not refresh saved DOCX field caches; load `refresh_fields` when requested.

Use ledger receipts to avoid repeating successful changes after compaction/restart. Verify the requested effect with bounded reads when needed. Repair stale evidence locally; reconcile uncertain execution before retrying. Finish when all requested work is staged. Rendered review is separate from text checks; do not claim vision from text. Leave Apply/Save to the user.
