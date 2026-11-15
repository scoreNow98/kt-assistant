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
-- Limits per plan are in main.py (PLANS). Change a company's plan with: python admin.py set-plan
ALTER TABLE organisations ADD COLUMN IF NOT EXISTS plan text NOT NULL DEFAULT 'trial';

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

-- Keyword search, for exact terms such as error codes and setting names. Filled in for existing rows too.
-- shortcut: English word stemming only; add a 'simple' column next to it when Hindi documents arrive.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', heading || ' ' || text)) STORED;
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);

-- Every question asked, with its answer and sources: chat history, feedback, and later the gap report.
CREATE TABLE IF NOT EXISTS messages (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  question text NOT NULL,
  searched_for text NOT NULL,
  answer text NOT NULL,
  sources jsonb NOT NULL,
  feedback smallint CHECK (feedback IN (-1, 1)),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_workspace_user ON messages (workspace_id, user_id, id);

-- The saved "Start here" guide, one per workspace.
CREATE TABLE IF NOT EXISTS guides (
  workspace_id bigint PRIMARY KEY REFERENCES workspaces (id) ON DELETE CASCADE,
  sections jsonb NOT NULL,
  reading_order jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Row-level security: client data is read and written by the kt_app login, which only sees workspaces
-- the signed-in person may open. The API filters too; this is the second lock.
--
-- Who is signed in comes from the session token the API puts in app.session, checked against the
-- sessions table by the functions below. kt_app cannot read sessions or users itself, so even code
-- that runs arbitrary SQL as kt_app can only act as a person whose secret session token it holds.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kt_app') THEN
    CREATE ROLE kt_app NOLOGIN;
  END IF;
END $$;

-- SECURITY DEFINER: these run as the table owner, so they can read sessions and users on kt_app's behalf.
-- Tables are named with public. and pg_temp is searched last, so a temporary table cannot stand in for them.
CREATE OR REPLACE FUNCTION kt_user_id() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT user_id FROM public.sessions
  WHERE token_hash = encode(sha256(convert_to(current_setting('app.session', true), 'UTF8')), 'hex')
    AND expires_at > now()
$$;

CREATE OR REPLACE FUNCTION kt_org_id() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT org_id FROM public.users WHERE id = public.kt_user_id()
$$;

CREATE OR REPLACE FUNCTION kt_is_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT role = 'admin' FROM public.users WHERE id = public.kt_user_id()), false)
$$;

CREATE OR REPLACE FUNCTION kt_is_member(ws bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM public.workspace_members WHERE workspace_id = ws AND user_id = public.kt_user_id())
$$;

CREATE OR REPLACE FUNCTION kt_in_my_org(person bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM public.users WHERE id = person AND org_id = public.kt_org_id())
$$;

REVOKE ALL ON FUNCTION kt_user_id(), kt_org_id(), kt_is_admin(), kt_is_member(bigint), kt_in_my_org(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kt_user_id(), kt_org_id(), kt_is_admin(), kt_is_member(bigint), kt_in_my_org(bigint) TO kt_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON workspaces, workspace_members, documents, chunks, messages, guides TO kt_app;
REVOKE ALL ON organisations, users, sessions, invites FROM kt_app;  -- phase 2-5 granted some user columns
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO kt_app;
DO $$ BEGIN
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE guides ENABLE ROW LEVEL SECURITY;

-- Same organisation, and either an admin or a member of that workspace. Reads the row's own values, so it
-- also decides which new workspaces may be created (admins, in their own organisation).
DROP POLICY IF EXISTS workspace_access ON workspaces;
CREATE POLICY workspace_access ON workspaces
  USING (org_id = kt_org_id() AND (kt_is_admin() OR kt_is_member(id)))
  WITH CHECK (org_id = kt_org_id() AND kt_is_admin());

DROP POLICY IF EXISTS membership_access ON workspace_members;
CREATE POLICY membership_access ON workspace_members
  USING (workspace_id IN (SELECT id FROM workspaces))
  WITH CHECK (kt_is_admin() AND workspace_id IN (SELECT id FROM workspaces) AND kt_in_my_org(user_id));

DROP POLICY IF EXISTS document_access ON documents;
CREATE POLICY document_access ON documents USING (workspace_id IN (SELECT id FROM workspaces));

DROP POLICY IF EXISTS chunk_access ON chunks;
CREATE POLICY chunk_access ON chunks USING (workspace_id IN (SELECT id FROM workspaces));

-- People see only their own questions, and only in workspaces they can still open.
DROP POLICY IF EXISTS message_access ON messages;
CREATE POLICY message_access ON messages USING (
  user_id = kt_user_id() AND workspace_id IN (SELECT id FROM workspaces)
);

DROP POLICY IF EXISTS guide_access ON guides;
CREATE POLICY guide_access ON guides USING (workspace_id IN (SELECT id FROM workspaces));

-- Deleting, and writing the guide, are for admins only, here as well as in the API.
DROP POLICY IF EXISTS admin_delete ON workspaces;
CREATE POLICY admin_delete ON workspaces AS RESTRICTIVE FOR DELETE USING (kt_is_admin());
DROP POLICY IF EXISTS admin_delete ON workspace_members;
CREATE POLICY admin_delete ON workspace_members AS RESTRICTIVE FOR DELETE USING (kt_is_admin());
DROP POLICY IF EXISTS admin_delete ON documents;
CREATE POLICY admin_delete ON documents AS RESTRICTIVE FOR DELETE USING (kt_is_admin());
DROP POLICY IF EXISTS admin_delete ON chunks;
CREATE POLICY admin_delete ON chunks AS RESTRICTIVE FOR DELETE USING (kt_is_admin());
DROP POLICY IF EXISTS admin_insert ON guides;
CREATE POLICY admin_insert ON guides AS RESTRICTIVE FOR INSERT WITH CHECK (kt_is_admin());
DROP POLICY IF EXISTS admin_update ON guides;
CREATE POLICY admin_update ON guides AS RESTRICTIVE FOR UPDATE USING (kt_is_admin());
