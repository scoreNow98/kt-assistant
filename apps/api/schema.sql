-- Applied on every API start; every statement must be safe to repeat.
-- shortcut: no migration tool; add one before the first change that must keep existing customer data.
CREATE EXTENSION IF NOT EXISTS vector;

-- Phase 1 stored the workspace as free text. Those two tables are replaced once; reload documents afterwards.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'documents' AND column_name = 'workspace') THEN
    DROP TABLE chunks, documents;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS organisations (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- shortcut: a user belongs to exactly one organisation; move org_id and role to a join table if someone needs two.
CREATE TABLE IF NOT EXISTS users (
  id bigserial PRIMARY KEY,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  org_id bigint NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('admin', 'member')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash text PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
  token_hash text PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('admin', 'member')),
  expires_at timestamptz NOT NULL,
  used_at timestamptz
);

CREATE TABLE IF NOT EXISTS workspaces (
  id bigserial PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES organisations (id) ON DELETE CASCADE,
  name text NOT NULL,
  UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS workspace_members (
  workspace_id bigint NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS documents (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
  name text NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
  id bigserial PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  workspace_id bigint NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
  heading text NOT NULL,
  page int,
  text text NOT NULL,
  embed_model text NOT NULL,
  -- shortcut: no fixed size and no vector index, so search scans every chunk in the workspace;
  -- fix the size and add an HNSW index when a workspace passes about 100k chunks.
  embedding vector NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_workspace_id ON chunks (workspace_id);

-- Row-level security: client data is read and written as the role kt_app, which only sees
-- workspaces the current user (app.user_id) may open. The API code filters too; this is the second lock.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kt_app') THEN
    CREATE ROLE kt_app NOLOGIN;
  END IF;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON workspaces, workspace_members, documents, chunks TO kt_app;
GRANT SELECT (id, email, org_id, role) ON users TO kt_app;  -- never the password hash
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO kt_app;

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;

-- Same organisation, and either an admin or a member of that workspace.
DROP POLICY IF EXISTS workspace_access ON workspaces;
CREATE POLICY workspace_access ON workspaces USING (
  EXISTS (
    SELECT 1 FROM users u
    WHERE u.id = current_setting('app.user_id', true)::bigint
      AND u.org_id = workspaces.org_id
      AND (u.role = 'admin' OR EXISTS (
        SELECT 1 FROM workspace_members m WHERE m.workspace_id = workspaces.id AND m.user_id = u.id))
  )
);

DROP POLICY IF EXISTS document_access ON documents;
CREATE POLICY document_access ON documents USING (workspace_id IN (SELECT id FROM workspaces));

DROP POLICY IF EXISTS chunk_access ON chunks;
CREATE POLICY chunk_access ON chunks USING (workspace_id IN (SELECT id FROM workspaces));
