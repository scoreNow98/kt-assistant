#!/usr/bin/env bash
# Downloads public docs of two open-source projects to act as two pretend clients.
# Result: sample-data/client-a (FastAPI docs) and sample-data/client-b (Ollama docs).
set -euo pipefail
cd "$(dirname "$0")/.."

# Pinned to fixed commits so eval.py scores stay comparable over time. Update both together on purpose.
fetch() {  # fetch <client folder> <github repo> <commit> <docs path inside the repo>
  local dest="sample-data/$1" tmp
  [ -d "$dest" ] && { echo "$dest already exists, skipping"; return; }
  tmp="$(mktemp -d)"
  git -C "$tmp" init --quiet
  git -C "$tmp" remote add origin "https://github.com/$2.git"
  git -C "$tmp" sparse-checkout set "$4"
  git -C "$tmp" fetch --quiet --depth 1 --filter=blob:none origin "$3"
  git -C "$tmp" checkout --quiet FETCH_HEAD
  mkdir -p sample-data
  mv "$tmp/$4" "$dest"
  rm -rf "$tmp"
  echo "$dest: $(find "$dest" -type f | wc -l) files"
}

fetch client-a fastapi/fastapi 2a1bd230b454a51644c7e2087f525b2dd245edbf docs/en/docs
fetch client-b ollama/ollama 15af2b261c14c494d1fe08cf9d0b9e0af1a18c84 docs
