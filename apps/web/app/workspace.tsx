"use client";

import { useEffect, useState } from "react";
import { api, post, stream, type Source, type Workspace } from "./api";
import Chat, { TurnView } from "./chat";

type View = "ask" | "guide" | "gaps" | "handover";
type Section = { title: string; answer: string; sources: Source[] };
type Guide = { sections: Section[]; reading_order: string[]; created_at: string };
type Gap = { question: string; asked: number; last_asked: string; not_found: boolean; not_helpful: boolean };

const VIEWS: [View, string][] = [
  ["ask", "Ask"],
  ["guide", "Start here"],
  ["gaps", "Gaps"],
  ["handover", "Hand over"],
];

const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

export default function WorkspaceView({ workspace, isAdmin }: { workspace: Workspace; isAdmin: boolean }) {
  const [view, setView] = useState<View>("ask");
  const nav = (
    <nav className="tabs" aria-label={`${workspace.name} sections`}>
      {VIEWS.map(([v, label]) => (
        <button key={v} aria-current={v === view ? "page" : undefined} onClick={() => setView(v)}>
          {label}
        </button>
      ))}
    </nav>
  );
  if (view === "ask") return <Chat workspace={workspace} nav={nav} />;
  return (
    <div className="panel">
      <header>
        <h1>{workspace.name}</h1>
        {nav}
      </header>
      <div className="panel-body">
        {view === "guide" && <GuidePage workspace={workspace} isAdmin={isAdmin} />}
        {view === "gaps" && <GapsPage workspace={workspace} onHandover={() => setView("handover")} />}
        {view === "handover" && <HandoverPage workspace={workspace} />}
      </div>
    </div>
  );
}

function GuidePage({ workspace, isAdmin }: { workspace: Workspace; isAdmin: boolean }) {
  const [guide, setGuide] = useState<Guide | null | undefined>(undefined);
  const [building, setBuilding] = useState<Section[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Guide | null>(`/workspaces/${workspace.id}/guide`).then(setGuide).catch((e) => setError(e.message));
  }, [workspace.id]);

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
      setError(e instanceof Error ? e.message : "Could not create the guide");
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
        {isAdmin && guide !== undefined && (
          <button className="primary" disabled={building !== null} onClick={build}>
            {guide ? "Update the guide" : "Create the guide"}
          </button>
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

function GapsPage({ workspace, onHandover }: { workspace: Workspace; onHandover: () => void }) {
  const [gaps, setGaps] = useState<Gap[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Gap[]>(`/workspaces/${workspace.id}/gaps`).then(setGaps).catch((e) => setError(e.message));
  }, [workspace.id]);

  return (
    <div className="kt">
      <div className="kt-intro">
        <h2>What the documents don't cover</h2>
        <p>Questions from the last 30 days that had no answer in the documents, or whose answer someone marked not helpful. Who asked is not shown.</p>
        {error && <p className="error" role="alert">{error}</p>}
      </div>
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

function HandoverPage({ workspace }: { workspace: Workspace }) {
  const [questions, setQuestions] = useState<{ q: string; gap: boolean }[] | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const standard = STANDARD_QUESTIONS.map((q) => ({ q, gap: false }));
    api<Gap[]>(`/workspaces/${workspace.id}/gaps`)
      .then((gaps) => setQuestions([...gaps.slice(0, 8).map((g) => ({ q: g.question, gap: true })), ...standard]))
      .catch(() => setQuestions(standard));
  }, [workspace.id]);

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
      setError(err instanceof Error ? err.message : "Could not save the notes");
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
