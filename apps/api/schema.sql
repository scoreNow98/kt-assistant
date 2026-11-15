-- Applied on every API start; every statement must be safe to repeat.
-- shortcut: no migration tool, so columns cannot be changed here; add one when the schema first needs altering.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id bigserial PRIMARY KEY,
  workspace text NOT NULL,
  name text NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace, sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
  id bigserial PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  workspace text NOT NULL,
  heading text NOT NULL,
  page int,
  text text NOT NULL,
  embed_model text NOT NULL,
  -- shortcut: no fixed size and no vector index, so search scans every chunk in the workspace;
  -- fix the size and add an HNSW index when a workspace passes about 100k chunks.
  embedding vector NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_workspace ON chunks (workspace);
