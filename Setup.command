#!/bin/zsh
set -eu
cd "${0:A:h}"
for dependency in uv npm cargo; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    print "Install $dependency first; see docs/REPRODUCE.md."
    read "?Press Enter to close."; exit 1
  fi
done
print "This installs locked Python and npm dependencies; Cargo downloads crates on first launch."
print "No model weights are downloaded. Word worker dependencies are a separate setup."
read "reply?Continue with dependency installation? [y/N] "
[[ "$reply" == [yY] ]] || exit 0
uv sync --frozen --extra mlx --python 3.12
npm ci
npm run build:web
print "Setup complete. Open Start LocalOffice.command to launch the real desktop client."
