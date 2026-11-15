#!/usr/bin/env bash
# Downloads public docs of two open-source projects to act as two pretend clients.
# Result: sample-data/client-a (FastAPI docs) and sample-data/client-b (Ollama docs).
set -euo pipefail
cd "$(dirname "$0")/.."

fetch() {  # fetch <client folder> <github repo> <docs path inside the repo>
  local dest="sample-data/$1" tmp
  [ -d "$dest" ] && { echo "$dest already exists, skipping"; return; }
  tmp="$(mktemp -d)"
  git clone --quiet --depth 1 --filter=blob:none --sparse "https://github.com/$2.git" "$tmp"
  git -C "$tmp" sparse-checkout set "$3"
  mkdir -p sample-data
  mv "$tmp/$3" "$dest"
  rm -rf "$tmp"
  echo "$dest: $(find "$dest" -type f | wc -l) files"
}

fetch client-a fastapi/fastapi docs/en/docs
fetch client-b ollama/ollama docs
