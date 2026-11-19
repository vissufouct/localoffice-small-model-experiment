# Public export review

Reviewed on 18 November 2026.

The public package uses an explicit top-level source/documentation allowlist. It excludes `.venv`, npm dependencies, Rust targets, .NET bin/obj, Java classes, runtime data, project databases, caches, temporary folders, model files and raw logs. Runtime and model paths in curated evidence are replaced with labelled placeholders. Raw memory exports and original project databases are not included.

Checks performed: known personal home/temp-path patterns, common credential-token patterns and private-key markers; no matches in the final source set. Main README/documentation relative links resolve. The ZIP CRC check and every final manifest SHA-256 passed. These are bounded checks, not a guarantee that every possible sensitive datum or licence issue has been found.

Curated evidence contains synthetic fixture prompts, tool calls and task identifiers; it is intentionally labelled as redacted explanatory evidence. Hashes inside those reports refer to historical originals. Only PACKAGE_MANIFEST.json verifies the final public bytes.

Use the source-only ZIP for upload or reproduction. The working preparation folder also contains ignored dependencies, a checkout-dependent debug client and disposable UI-test state to make it runnable locally. Do not drag the entire working folder into GitHub's uploader. Git users must respect .gitignore and review staged files. Any future model registration writes a personal local path: restore the placeholder manifest before publishing.

The initial packaging on 18 November did not publish externally or run model inference. On 19 November the author authorized publication of the clean source repository and scoped CSV results.
