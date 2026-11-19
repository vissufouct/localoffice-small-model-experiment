# Desktop UI demo

The real Tauri client is included with `Setup.command` and `Start LocalOffice.command`. Install prerequisites once, then launch directly from this folder. It is a source-launched client, not a bundled installer. The research focus is harnessing small models; the UI makes that experiment accessible. There is no simulated inference labelled as a real model run.

After completing REPRODUCE.md, launch `npm run dev`. Create a new empty demo project outside the source checkout. Create two chats and confirm each keeps its own conversation while artifacts remain project-visible. Attach a synthetic text/PDF file; it is copied into the chat's input folder. Explore preview, Models & settings, Plugins & skills, and Memory & context.

For inference, discover and load your existing model. Start with a disposable Markdown request. Compare live/background previews, pause, annotate, continue, then explicitly Save. Review Undo before applying it. A model failure or refusal is part of the experiment and must remain visible.

For Word, install/build the worker and renderer first. The UI does not make Word creation reliable; use the frozen B1 fixture to observe the current failure mode. Record the actual artifact revision for any visual review. Do not claim the model has seen a page unless a vision-capable reviewer actually consumed its rendered image.

A local debug app is built under `apps/desktop/src-tauri/target/debug/bundle/macos/LocalOffice Experiment.app` on the preparation Mac; it was launched and project creation was verified. It depends on this checkout and is excluded from the public archive. Actual client screenshots are included in [the screenshot gallery](screenshots/README.md). No prerecorded video, self-contained distribution binary or model weights are included. A browser-only Vite launch displays frontend assets but cannot replace the native host connection.
