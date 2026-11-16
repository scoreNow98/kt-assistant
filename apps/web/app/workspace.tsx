"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, day, errorText, lostAccess, post, stream, type Doc, type Source, type Workspace } from "./api";
import Chat, { TurnView } from "./chat";

type View = "ask" | "guide" | "gaps" | "handover";
type Section = { title: string; answer: string; sources: Source[] };
type Guide = { sections: Section[]; reading_order: string[]; created_at: string; stale?: boolean };
type Gap = { question: string; asked: number; last_asked: string; not_found: boolean; not_helpful: boolean };

const VIEWS: [View, string][] = [
  ["ask", "Ask"],
  ["guide", "Start here"],
  ["gaps", "Gaps"],
  ["handover", "Hand over"],
];

type Props = {
  workspace: Workspace;
  isAdmin: boolean;
  onChanged: () => void; // renamed: reload the workspace list
  onDeleted: () => void;
  onLostAccess: () => void;
};

export default function WorkspaceView({ workspace, isAdmin, onChanged, onDeleted, onLostAccess }: Props) {
  const [view, setView] = useState<View>("ask");
  // Several calls can answer 404 at once; report lost access once, through a stable callback.
  const latest = useRef(onLostAccess);
  const reported = useRef(false);
  useEffect(() => {
    latest.current = onLostAccess;
  }, [onLostAccess]);
  const lose = useCallback(() => {
    if (reported.current) return;
    reported.current = true;
    latest.current();
  }, []);
  const title = <WorkspaceTitle workspace={workspace} isAdmin={isAdmin} onChanged={onChanged} onDeleted={onDeleted} onLostAccess={lose} />;
  const nav = (
    <nav className="tabs" aria-label={`${workspace.name} sections`}>
      {VIEWS.map(([v, label]) => (
        <button key={v} aria-current={v === view ? "page" : undefined} onClick={() => setView(v)}>
          {label}
        </button>
      ))}
    </nav>
  );
  return (
    <>
      {/* The chat stays mounted on other tabs, so a running answer, the follow-up context and the draft survive. */}
      <div hidden={view !== "ask"}>
        <Chat
          workspace={workspace}
          nav={nav}
          title={title}
          isAdmin={isAdmin}
          active={view === "ask"}
          onLostAccess={lose}
          onHandover={() => setView("handover")}
        />
      </div>
      {view !== "ask" && (
        <div className="panel">
          <header>
            {title}
            {nav}
          </header>
          <div className="panel-body">
            {view === "guide" && <GuidePage workspace={workspace} isAdmin={isAdmin} onLostAccess={lose} />}
            {view === "gaps" && <GapsPage workspace={workspace} isAdmin={isAdmin} onLostAccess={lose} onHandover={() => setView("handover")} />}
            {view === "handover" && <HandoverPage workspace={workspace} onLostAccess={lose} />}
          </div>
        </div>
      )}
    </>
  );
}

/** The workspace name, with Rename and Delete workspace for admins. */
function WorkspaceTitle({ workspace, isAdmin, onChanged, onDeleted, onLostAccess }: Props) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(workspace.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const id = useId();

  function failed(e: unknown, fallback: string) {
    if (lostAccess(e)) onLostAccess();
    else setError(errorText(e, fallback));
  }

  async function rename(e: React.FormEvent) {
    e.preventDefault();
    const clean = name.trim().replace(/\s+/g, " ");
    if (clean === workspace.name) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api(`/workspaces/${workspace.id}`, { method: "PATCH", body: JSON.stringify({ name: clean }) });
      setEditing(false);
      onChanged();
    } catch (err) {
      failed(err, "Could not rename the workspace. Try again.");
    }
    setBusy(false);
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      const count = (await api<Doc[]>(`/workspaces/${workspace.id}/documents`)).length;
      const docs = count === 1 ? "its 1 document" : `its ${count} documents`;
      if (window.confirm(`Delete ${workspace.name} with ${docs}, everyone's questions and its guide? This cannot be undone.`)) {
        try {
          await api(`/workspaces/${workspace.id}`, { method: "DELETE" });
        } catch (err) {
          if (!lostAccess(err)) throw err; // 404: someone else deleted it already
        }
        onDeleted();
        return;
      }
    } catch (err) {
      failed(err, "Could not delete the workspace. Try again.");
    }
    setBusy(false);
  }

  if (editing)
    return (
      <form className="ws-title" onSubmit={rename}>
        <label htmlFor={id} className="visually-hidden">Workspace name</label>
        <input id={id} autoFocus value={name} onChange={(e) => setName(e.target.value)} required maxLength={60} />
        <button className="primary" disabled={busy || !name.trim()}>Save name</button>
        <button type="button" onClick={() => setEditing(false)}>Cancel</button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    );
  return (
    <div className="ws-title">
      <h1>{workspace.name}</h1>
      {isAdmin && (
        <>
          <button
            className="link"
            onClick={() => {
              setName(workspace.name);
              setError("");
              setEditing(true);
            }}
          >
            Rename
          </button>
          <button className="link" disabled={busy} onClick={remove}>
            Delete workspace
          </button>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}

function GuidePage({ workspace, isAdmin, onLostAccess }: { workspace: Workspace; isAdmin: boolean; onLostAccess: () => void }) {
  const [guide, setGuide] = useState<Guide | null | undefined>(undefined);
  const [building, setBuilding] = useState<Section[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Guide | null>(`/workspaces/${workspace.id}/guide`)
      .then(setGuide)
      .catch((e) => (lostAccess(e) ? onLostAccess() : setError(e.message)));
  }, [workspace.id, onLostAccess]);

  async function build() {
    setBuilding([]);
    setError("");
    try {
      await stream(`/workspaces/${workspace.id}/guide`, {}, (line) => {
        if ("section" in line) setBuilding((s) => [...(s ?? []), line.section as Section]);
        if ("error" in line) setError(line.error as string);
      });
      setGuide(await api<Guide | null>(`/workspaces/${workspace.id}/guide`));
    } catch (e) {
      if (lostAccess(e)) onLostAccess();
      else setError(errorText(e, "Could not create the guide"));
    }
    setBuilding(null);
  }

  const sections = building ?? guide?.sections ?? [];
  return (
    <div className="kt">
      <div className="kt-intro">
        <h2>Start here</h2>
        {guide === undefined && !error && <p>Loading…</p>}
        {guide === null && !building && (
          <p>
            {isAdmin
              ? "No guide yet. Creating one asks five onboarding questions of this workspace's documents and saves the cited answers. With a local model this takes a few minutes."
              : "No guide yet. An admin can create one from this page."}
          </p>
        )}
        {guide && !building && <p>Written from this workspace's documents on {day(guide.created_at)}. Update it after adding important documents.</p>}
        {building && <p role="status">Writing section {building.length + 1}. Each section is saved only when all are done.</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {((isAdmin && guide !== undefined) || (guide?.stale && !building)) && (
          <div className="row">
            {isAdmin && guide !== undefined && (
              <button className="primary" disabled={building !== null} onClick={build}>
                {guide ? "Update the guide" : "Create the guide"}
              </button>
            )}
            {guide?.stale && !building && <p className="stale">Documents changed since this guide was written.</p>}
          </div>
        )}
      </div>
      {guide && !building && guide.reading_order.length > 0 && (
        <section className="reading">
          <h3>Read these first</h3>
          <ol>
            {guide.reading_order.map((doc) => (
              <li key={doc}>{doc}</li>
            ))}
          </ol>
        </section>
      )}
      {sections.map((s) => (
        <TurnView key={s.title} turn={{ key: s.title, question: s.title, answer: s.answer, sources: s.sources }} />
      ))}
    </div>
  );
}

function GapsPage({
  workspace,
  isAdmin,
  onLostAccess,
  onHandover,
}: {
  workspace: Workspace;
  isAdmin: boolean;
  onLostAccess: () => void;
  onHandover: () => void;
}) {
  const [gaps, setGaps] = useState<Gap[] | null>(null);
  const [error, setError] = useState("");
  const [dismissed, setDismissed] = useState<string[]>([]); // shown as "Dismissed" for a moment, then removed
  const [failed, setFailed] = useState<{ question: string; text: string } | null>(null);
  const [announce, setAnnounce] = useState("");

  useEffect(() => {
    api<Gap[]>(`/workspaces/${workspace.id}/gaps`)
      .then(setGaps)
      .catch((e) => (lostAccess(e) ? onLostAccess() : setError(e.message)));
  }, [workspace.id, onLostAccess]);

  async function dismiss(question: string) {
    setFailed(null);
    try {
      await api(`/workspaces/${workspace.id}/gaps/dismiss`, post({ question }));
      setDismissed((d) => [...d, question]);
      setAnnounce(`Dismissed "${question}".`);
      setTimeout(() => {
        setGaps((all) => all?.filter((g) => g.question !== question) ?? all);
        setDismissed((d) => d.filter((q) => q !== question));
      }, 1500);
    } catch (e) {
      if (lostAccess(e)) onLostAccess();
      else setFailed({ question, text: errorText(e, "Could not dismiss this question. Try again.") });
    }
  }

  return (
    <div className="kt">
      <div className="kt-intro">
        <h2>What the documents don't cover</h2>
        <p>Questions from the last 30 days that had no answer in the documents, or whose answer someone marked not helpful. Who asked is not shown.</p>
        {isAdmin && <p>Dismiss a question that is not worth answering, such as a test or a typo. It stops showing here.</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      <p className="visually-hidden" role="status">{announce}</p>
      {gaps?.length === 0 && <p className="quiet">No gaps in the last 30 days.</p>}
      {gaps && gaps.length > 0 && (
        <>
          <ul className="gaps">
            {gaps.map((g) => (
              <li key={g.question}>
                <p className="q">{g.question}</p>
                <p className="meta">
                  {g.not_found ? "Not in the documents" : "Answer marked not helpful"}. Asked {g.asked === 1 ? "once" : `${g.asked} times`}, last on {day(g.last_asked)}.
                </p>
                {isAdmin &&
                  (dismissed.includes(g.question) ? (
                    <p className="meta">Dismissed</p>
                  ) : (
                    <button className="link dismiss" aria-label={`Dismiss "${g.question}"`} onClick={() => dismiss(g.question)}>
                      Dismiss
                    </button>
                  ))}
                {failed?.question === g.question && <p className="error" role="alert">{failed.text}</p>}
              </li>
            ))}
          </ul>
          <p className="kt-next">
            Know some of these? <button className="link" onClick={onHandover}>Answer them under Hand over</button>, and the answers become searchable.
          </p>
        </>
      )}
    </div>
  );
}

const STANDARD_QUESTIONS = [
  "What breaks most often, and how do you fix it?",
  "Who are the key people at the client, and what does each of them own?",
  "What do you do from memory that is not written down anywhere?",
  "What is in progress right now, and what is the next step?",
  "Anything else a newcomer should know?",
];

function HandoverPage({ workspace, onLostAccess }: { workspace: Workspace; onLostAccess: () => void }) {
  const [questions, setQuestions] = useState<{ q: string; gap: boolean }[] | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const standard = STANDARD_QUESTIONS.map((q) => ({ q, gap: false }));
    api<Gap[]>(`/workspaces/${workspace.id}/gaps`)
      .then((gaps) => setQuestions([...gaps.slice(0, 8).map((g) => ({ q: g.question, gap: true })), ...standard]))
      .catch((e) => {
        if (lostAccess(e)) onLostAccess();
        setQuestions(standard);
      });
  }, [workspace.id, onLostAccess]);

  const filled = Object.values(answers).some((a) => a.trim());

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api<{ name: string }>(
        `/workspaces/${workspace.id}/handover`,
        post({ answers: (questions ?? []).map(({ q }) => ({ question: q, answer: answers[q] ?? "" })) }),
      );
      setResult(`Saved as ${r.name}. Everyone with access to ${workspace.name} can now ask about it.`);
      setAnswers({});
    } catch (err) {
      if (lostAccess(err)) onLostAccess();
      else setError(errorText(err, "Could not save the notes"));
    }
    setBusy(false);
  }

  return (
    <form className="kt handover" onSubmit={save}>
      <div className="kt-intro">
        <h2>Hand over what you know</h2>
        <p>Leaving the project, or the person who knows something nobody wrote down? Answer what you can and skip the rest. Your answers are saved as a document under your name, and answers cite it.</p>
      </div>
      {questions?.map(({ q, gap }, i) => (
        <label key={q} className={gap ? "gap" : undefined}>
          <span>{q}</span>
          {gap && i === 0 && <small>The team asked these and the documents had no answer.</small>}
          <textarea rows={3} value={answers[q] ?? ""} onChange={(e) => setAnswers((a) => ({ ...a, [q]: e.target.value }))} maxLength={10000} />
        </label>
      ))}
      {error && <p className="error" role="alert">{error}</p>}
      <p role="status" className="quiet">{result}</p>
      <button className="primary" disabled={busy || !filled}>{busy ? "Saving…" : "Save handover notes"}</button>
    </form>
  );
}
