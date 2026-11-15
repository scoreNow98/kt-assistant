"""Bulk-load a folder into a workspace:  python ingest.py <workspace> <folder>"""
import sys
from pathlib import Path

import rag

workspace, folder = sys.argv[1], Path(sys.argv[2])
rag.init_schema()
files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in rag.SUFFIXES)
total = 0
for i, path in enumerate(files, 1):
    try:
        result = rag.ingest(workspace, str(path.relative_to(folder)), path.read_bytes())
    except ValueError as e:  # one unreadable file must not stop the batch
        result = {"status": f"skipped ({e})", "chunks": 0}
    total += result["chunks"]
    print(f"[{i}/{len(files)}] {path.relative_to(folder)}: {result['status']}, {result['chunks']} chunks", flush=True)
print(f"done: {total} new chunks in workspace {workspace}")
