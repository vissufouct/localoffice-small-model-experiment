# Setup and reproduction

## 1. Real desktop client

Supported development target: Apple-silicon macOS. Install Node.js 22.13+ (24+ supported), uv, Python 3.11–3.13, Rust/Cargo and Xcode command-line tools. No Microsoft Office installation is required to launch the client. No installers or model weights are included.

Open `Setup.command` once, then `Start LocalOffice.command`. Alternatively run from the repository root:

```sh
uv sync --frozen --extra mlx --python 3.12
npm ci
npm run dev
```

The setup asks before installing dependencies. First desktop launch may download Cargo dependencies and compile the shell. macOS may require permission to access a chosen project folder. Use a new disposable project. The app starts an authenticated loopback host; do not expose it publicly.

This is a source-launched client, not a notarized, standalone `.app`. The exported folder must remain in place for a development build to run. `npm run web` alone cannot connect to the native host. Existing models are discovered in the UI; model loading/generation is optional for exploring the interface. Install llama-server separately if using the llama.cpp adapter. MLX requires Apple silicon.

## 2. Optional Word execution

The worker pins .NET SDK **10.0.404**, Apple-silicon macOS. Obtain the SDK from the official URL in `docs/benchmarks/dotnet-install.json`, verify the listed SHA-512, and extract it into `.runtime/dotnet` so `.runtime/dotnet/dotnet` exists. The recorded archive size is 233,588,093 bytes. Installation is explicit; no models are included.

```sh
.venv/bin/python scripts/build_word_worker.py --restore
```

This restores the locked NuGet dependencies and builds the Open XML worker. Install LibreOffice at `/Applications/LibreOffice.app` for PDF rendering. Install the requested fonts, including Arial, under their own licences; substitution can affect layout. Field/TOC updates additionally require the pinned Java helper:

```sh
.venv/bin/python scripts/install_field_runtime.py
.venv/bin/python scripts/build_field_worker.py
```

The Java installer downloads a checksum-pinned library archive of about 200 MB. OCR requires Tesseract and appropriate language data; it is separate from embedded PDF text extraction. Do not treat missing dependencies as model failures.

## 3. Register the frozen model

Acquire `mlx-community/Qwen3-4B-Instruct-2507-4bit` revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b` separately under its own licence, or reuse existing weights. No script here should silently fetch or substitute weights. Register your existing snapshot:

```sh
.venv/bin/python scripts/configure_local_model.py /absolute/path/to/snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b
```

The registration checks directory structure, not model hashes. It writes your local path to `docs/benchmarks/approved-mlx-download.json`; never publish that modified file. Verify weights independently if making reproducibility claims. Discovery/registration does not certify compatibility.

## 4. Fresh complete-task benchmark

Unload any model in the desktop app first. Run one inference workload at a time; avoid concurrent builds/tests/rendering. From the repository root:

```sh
PYTHONPATH=host:scripts .venv/bin/python scripts/benchmark_word_body.py \
  --output docs/benchmarks/my-b1-run \
  --requirements-planning --focused-drafting --isolated-draft-planning
```

Choose a new output folder each time. The frozen fixture and stricter verifier remain unchanged. Nonzero exit may be an expected experimental failure. Exit zero means **machine checks passed, human/model semantic and actual rendered visual review still pending**, not complete acceptance. Preserve original reports; record exact revision and reviewer. Do not silently increase step/token budgets or substitute a model to rescue a run.

Historical saved-state probes depend on excluded private temporary databases and reports. They are retained as research source, not promised as runnable commands. Public `docs/evidence` files are redacted explanatory copies; hash references inside them refer to the original evidence.

## 5. Validation and measurement

```sh
npm run build:web
uv run --frozen --extra mlx pytest -q
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
```

Some tests need built Word workers, optional runtimes or the pinned tokenizer. Record missing dependencies, skips and failures; do not count skipped cases as passes. Historical 2799-pass evidence was collected in the original configured workspace, not a clean installation of this export.

For new model experiments record the exact model revision, quantization, backend/template/parser/grammar versions, sampling, input/output reserves, task tokens, calls/retries, elapsed time, memory and artifact validity. Physical 16 GB qualification remains unperformed. Separate deterministic tool tests, UI behaviour, real-model tasks, semantic review, rendered review and Microsoft Word compatibility.

## 6. Produce a clean public source archive

```sh
.venv/bin/python scripts/package_public.py --output ../localoffice-source-new.zip
```

The destination must not already exist. The packager excludes installed dependencies, debug apps, model weights, project databases and runtime logs. It rejects known private-path/credential patterns and records file hashes. Review the resulting archive before publication. The local debug client remains available in the working folder.
