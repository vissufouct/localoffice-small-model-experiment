# Export validation — 18 November 2026

- Frontend: TypeScript and Vite production build passed after the final UI archival label change. Bundle-size warning remains (main JS about 571 kB).
- Native desktop: debug Tauri `.app` built successfully from this folder (about 29 MiB). Opened the real `LocalOffice Experiment` app; observed the project/chat/composer/preview UI, created a disposable project through the UI, and verified the “Ready for this chat” state. No inference was needed or claimed.
- Export has a separate app identifier and default settings directory (`~/.localoffice-experiment`). The start launcher explicitly uses `.runtime/demo-settings`. Original app settings are not reused.
- Final targeted checks: draft-placement verifier, post-creation context handling and export settings override: **24 passed in 1.98 seconds**, using this folder's Python environment.
- Python 3.12 and npm dependencies installed from existing local caches. An initial offline Python 3.13 attempt failed because a wheel was not cached; setup now selects Python 3.12. No model weights were downloaded.
- Both macOS launcher scripts passed shell syntax validation. First-run setup on another Mac remains unverified.
- No new model accuracy test, full suite run, Word worker bootstrap or physical 16 GB qualification was performed. Historical 2799-test evidence remains separate.
- Local ignored dependencies and build products make this folder runnable here. They are excluded from the source-only public archive by `scripts/package_public.py`. The debug app is checkout-dependent and is not a portable binary distribution.
- Source-export manifest records original copied bytes. Export-specific changes include app identity/archival label, isolated settings, model-registration helper, setup/launch scripts and documentation. PACKAGE_MANIFEST.json inside the archive records final exported bytes.

Known-pattern privacy checks are not an exhaustive security guarantee. Curated evidence is redacted, explicitly labelled and not a byte-identical saved-state replay.
