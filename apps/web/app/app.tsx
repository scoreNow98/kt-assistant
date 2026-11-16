"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, post, type Health, type Me } from "./api";
import Team from "./team";
import WorkspaceView from "./workspace";

export default function App({ health }: { health: Health }) {
  const [me, setMe] = useState<Me | null | undefined>(undefined); // undefined = still loading
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [view, setView] = useState<"chat" | "team">("chat");
  const [lost, setLost] = useState(""); // name of a workspace this person can no longer open

  const reload = useCallback(async () => {
    try {
      setMe(await api<Me>("/me"));
      setLoadError("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setMe(null);
      else setLoadError(e instanceof Error ? e.message : "Something went wrong");
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  if (loadError) return <p className="notice" role="alert">{loadError}</p>;
  if (me === undefined) return <p className="notice">Loading…</p>;
  if (me === null) return <SignIn onDone={reload} />;

  const workspace = me.workspaces.find((w) => w.id === selected) ?? me.workspaces[0];
  const healthy = health.api === "ok" && health.db === "ok" && health.pgvector;

  return (
    <div className="shell">
      <nav className="side" aria-label="Workspaces">
        <p className="org">{me.org}</p>
        <h2>Client workspaces</h2>
        <ul className="workspaces">
          {me.workspaces.map((w) => (
            <li key={w.id}>
              <button
                aria-current={view === "chat" && w.id === workspace?.id ? "page" : undefined}
                onClick={() => {
                  setSelected(w.id);
                  setView("chat");
                }}
              >
                {w.name}
              </button>
            </li>
          ))}
        </ul>
        {me.role === "admin" && (
          <>
            <NewWorkspace
              onCreated={async (id) => {
                await reload();
                setSelected(id);
                setView("chat");
              }}
            />
            <button className="link" aria-current={view === "team" ? "page" : undefined} onClick={() => setView("team")}>
              Team and access
            </button>
          </>
        )}
        <div className="account">
          {!healthy && <p className="warn">The server reports a problem with the database.</p>}
          <p title={me.email}>{me.email}</p>
          <button
            className="link"
            onClick={async () => {
              await api("/auth/logout", { method: "POST" }).catch(() => {});
              setMe(null);
            }}
          >
            Sign out
          </button>
        </div>
      </nav>
      <main>
        {lost && (
          <div className="lost-access" role="alert">
            <p>You no longer have access to {lost}. Ask an admin if you still need it.</p>
            <button className="link" onClick={() => setLost("")}>Close</button>
          </div>
        )}
        {view === "team" && me.role === "admin" ? (
          <Team workspaces={me.workspaces} />
        ) : workspace ? (
          <WorkspaceView
            key={workspace.id}
            workspace={workspace}
            isAdmin={me.role === "admin"}
            onChanged={reload}
            onDeleted={async () => {
              setSelected(null);
              await reload();
            }}
            onLostAccess={() => {
              setLost(workspace.name);
              setSelected(null);
              reload();
            }}
          />
        ) : (
          <div className="empty">
            <h1>No workspaces yet</h1>
            <p>
              {me.role === "admin"
                ? "Create a workspace for each client, then add that client's documents to it."
                : "Your admin has not added you to a workspace yet. Ask them for access."}
            </p>
          </div>
        )}
      </main>
    </div>
  );
}

function NewWorkspace({ onCreated }: { onCreated: (id: number) => void }) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  return (
    <form
      className="new-workspace"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          const w = await api<{ id: number }>("/workspaces", post({ name }));
          setName("");
          setError("");
          onCreated(w.id);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not create the workspace");
        }
      }}
    >
      <label htmlFor="new-workspace">New workspace</label>
      <div className="row">
        <input id="new-workspace" value={name} onChange={(e) => setName(e.target.value)} placeholder="Client name" required maxLength={60} />
        <button>Create</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
    </form>
  );
}

function SignIn({ onDone }: { onDone: () => void }) {
  const [invite, setInvite] = useState<string | null>(null);
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("invite");
    if (token) {
      setInvite(token);
      setMode("signup");
    }
  }, []);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const body = { email: form.get("email"), password: form.get("password"), org_name: form.get("org_name") || undefined, invite: invite ?? undefined };
    setBusy(true);
    try {
      await api(mode === "signup" ? "/auth/signup" : "/auth/login", post(body));
      if (invite) window.history.replaceState(null, "", window.location.pathname);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  const joining = mode === "signup" && invite;
  return (
    <div className="signin">
      <div className="pitch">
        <p className="brand">KT Assistant</p>
        <h1>Ask the project. Read the source.</h1>
        <p>Knowledge transfer for service teams. One workspace per client, built from the documents you already have.</p>
        <dl className="features">
          <div>
            <dt>Ask</dt>
            <dd>Answers are written from that client's documents and cite the passages they used, so you can check them.</dd>
          </div>
          <div>
            <dt>Start here</dt>
            <dd>A new joiner gets a cited guide and a reading order on day one.</dd>
          </div>
          <div>
            <dt>Gaps and handover</dt>
            <dd>See what the documents never covered, and capture it from the person who knows before they leave.</dd>
          </div>
          <div>
            <dt>Walls between clients</dt>
            <dd>People see only the clients they work on. The database enforces it, not just the app.</dd>
          </div>
        </dl>
      </div>
      <form onSubmit={submit}>
        <h2>{joining ? "Join your team" : mode === "signup" ? "Create your company account" : "Sign in"}</h2>
        {mode === "signup" && !invite && (
          <label>
            Company name
            <input name="org_name" required maxLength={100} autoComplete="organization" />
          </label>
        )}
        <label>
          Work email
          <input name="email" type="email" required autoComplete="email" />
        </label>
        <label>
          Password
          <input name="password" type="password" required minLength={8} autoComplete={mode === "signup" ? "new-password" : "current-password"} />
          {mode === "signup" && <small>At least 8 characters.</small>}
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>
          {joining ? "Join team" : mode === "signup" ? "Create account" : "Sign in"}
        </button>
        {!invite && (
          <button
            type="button"
            className="link"
            onClick={() => {
              setMode(mode === "signup" ? "signin" : "signup");
              setError("");
            }}
          >
            {mode === "signup" ? "I already have an account" : "Create a company account"}
          </button>
        )}
      </form>
    </div>
  );
}
