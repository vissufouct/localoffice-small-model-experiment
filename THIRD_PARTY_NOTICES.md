# Third-party components

Original LocalOffice experiment code is MIT licensed. Dependencies are obtained separately and retain their own licences; the project MIT licence does not relicense them.

| Component | Role | Source |
|---|---|---|
| Tauri | Desktop shell | https://github.com/tauri-apps/tauri |
| Vite / TypeScript | Frontend build | https://github.com/vitejs/vite / https://github.com/microsoft/TypeScript |
| PDF.js, DOMPurify, marked | PDF/Markdown previews and sanitization | npm package metadata and package-lock.json |
| FastAPI, Uvicorn, Pydantic dependencies | Local host and contracts | uv.lock and upstream package distributions |
| MLX / MLX-LM / Outlines | Apple-silicon inference and constrained output | uv.lock and upstream distributions |
| llama.cpp | Optional external inference server | https://github.com/ggml-org/llama.cpp |
| Open XML SDK 3.5.1 | Deterministic Word operations | https://github.com/dotnet/Open-XML-SDK |
| .NET SDK | External worker runtime | https://github.com/dotnet/sdk |
| LibreOffice | External rendering and field updates | https://www.libreoffice.org/ |
| Eclipse Temurin | Optional external Java runtime | https://adoptium.net/ |
| Tesseract / language data | Optional external OCR | https://github.com/tesseract-ocr |
| Qwen3 model weights | Separately acquired experimental model | https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit |

See `docs/npm-dependency-licences.json` for licence metadata recorded in the npm lockfile. Python, Cargo and NuGet dependency versions are recorded in their committed lockfiles. This is an attribution map, not a legal clearance or complete transitive licence audit. No dependency binaries, model weights, fonts or runtime installers are redistributed. Any future binary bundle needs its own complete licence/notice audit.
