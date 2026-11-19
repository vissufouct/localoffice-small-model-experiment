#!/bin/zsh
set -eu
cd "${0:A:h}"
for dependency in uv npm cargo; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    print "Missing $dependency. Open docs/REPRODUCE.md for setup instructions."
    read "?Press Enter to close."; exit 1
  fi
done
if [[ ! -x .venv/bin/python || ! -d node_modules ]]; then
  print "First launch needs Python/JavaScript dependencies. No models will be downloaded."
  print "Run ./Setup.command first, then reopen this launcher."
  read "?Press Enter to close."; exit 1
fi
export LOCALOFFICE_SETTINGS_DIR="$PWD/.runtime/demo-settings"
exec npm run dev
