"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, stream, type Message, type Source, type Workspace } from "./api";

export type Turn = {
  key: string;
  question: string;
  searchedFor?: string;
  answer?: string;
  sources?: Source[];
  error?: string;
  messageId?: number;
  feedback?: -1 | 1 | null;
  streaming?: boolean;
};
type Doc = { name: string; chunks: number };

const CITATION = /\[(\d+(?:\s*,\s*\d+)*)\]/;
const TOKENS = /(\[\d+(?:\s*,\s*\d+)*\]|`[^`\n]+`|\*\*[^*\n]+\*\*)/;

const citedNumbers = (answer: string) =>
  new Set([...answer.matchAll(new RegExp(CITATION, "g"))].flatMap((m) => m[1].split(",").map((n) => Number(n.trim()))));

const fromMessage = (m: Message): Turn => ({
  key: `m${m.id}`,
  question: m.question,
  searchedFor: m.searched_for,
  answer: m.answer,
  sources: m.sources,
  messageId: m.id,
  feedback: m.feedback,
});

export default function Chat({ workspace, nav }: { workspace: Workspace; nav: React.ReactNode }) {
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [upload, setUpload] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const end = useRef<HTMLDivElement>(null);

  const loadDocs = useCallback(
    () => api<Doc[]>(`/workspaces/${workspace.id}/documents`).then(setDocs).catch(() => setDocs([])),
    [workspace.id],
  );
  useEffect(() => {
    loadDocs();
    api<Message[]>(`/workspaces/${workspace.id}/messages`)
      .then((ms) => setTurns(ms.map(fromMessage)))
      .catch(() => {});
  }, [loadDocs, workspace.id]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [turns.length]);

  const patch = (key: string, change: (t: Turn) => Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.key === key ? { ...t, ...change(t) } : t)));

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const text = question.trim();
    if (!text || busy) return;
    // The last two answered turns let the server understand a follow-up such as "and how do I restart it?".
    const history = turns
      .filter((t) => t.answer && !t.error && !t.streaming)
      .slice(-2)
      .map((t) => ({ question: t.question, answer: t.answer }));
    const key = `q${Date.now()}`;
    setTurns((all) => [...all, { key, question: text, streaming: true }]);
    setQuestion("");
    setBusy(true);
    try {
      await stream(`/workspaces/${workspace.id}/ask`, { question: text, history }, (line) => {
        if ("sources" in line) patch(key, () => ({ searchedFor: line.searched_for as string, sources: line.sources as Source[] }));
        else if ("delta" in line) patch(key, (t) => ({ answer: (t.answer ?? "") + (line.delta as string) }));
        else if ("error" in line) patch(key, () => ({ error: line.error as string }));
        else if ("done" in line) patch(key, () => ({ messageId: line.message_id as number, feedback: null }));
      });
    } catch (err) {
      patch(key, () => ({ error: err instanceof Error ? err.message : "Something went wrong" }));
    }
    patch(key, () => ({ streaming: false }));
    setBusy(false);
  }

  async function rate(turn: Turn, value: -1 | 1) {
    const next = turn.feedback === value ? null : value;
    patch(turn.key, () => ({ feedback: next }));
    try {
      await api(`/messages/${turn.messageId}/feedback`, { method: "PUT", body: JSON.stringify({ value: next ?? 0 }) });
    } catch {
      patch(turn.key, () => ({ feedback: turn.feedback }));
    }
  }

  async function addFiles(files: FileList) {
    let stored = 0;
    const problems: string[] = [];
    for (const file of Array.from(files)) {
      setUpload(`Reading ${file.name}…`);
      const form = new FormData();
      form.append("file", file);
      try {
        const r = await api<{ status: string }>(`/workspaces/${workspace.id}/documents`, { method: "POST", body: form });
        if (r.status === "duplicate") problems.push(`${file.name} is already here`);
        else stored += 1;
      } catch (err) {
        problems.push(`${file.name}: ${err instanceof Error ? err.message : "could not be read"}`);
      }
    }
    setUpload([stored ? `${stored} document${stored === 1 ? "" : "s"} added.` : "", ...problems].filter(Boolean).join(" "));
    if (fileInput.current) fileInput.current.value = "";
    loadDocs();
  }

  return (
    <div className="chat">
      <header>
        <h1>{workspace.name}</h1>
        <details className="docs">
          <summary>{docs === null ? "Loading documents…" : `${docs.length} document${docs.length === 1 ? "" : "s"}`}</summary>
          <ul>{docs?.map((d) => <li key={d.name}>{d.name}</li>)}</ul>
        </details>
        <button onClick={() => fileInput.current?.click()}>Add documents</button>
        <input ref={fileInput} type="file" hidden multiple accept=".md,.mdx,.txt,.pdf,.docx" onChange={(e) => e.target.files?.length && addFiles(e.target.files)} />
        {nav}
        <p className="upload-status" role="status">{upload}</p>
      </header>

      <div className="thread">
        {turns.length === 0 && (
          <div className="empty">
            {docs?.length === 0 ? (
              <>
                <h2>This workspace has no documents yet</h2>
                <p>Add runbooks, handover notes, meeting notes or design documents. Markdown, text, PDF and Word files work.</p>
              </>
            ) : (
              <>
                <h2>Ask anything about {workspace.name}</h2>
                <p>Answers come only from this workspace's documents, with the source of each statement beside it. You can ask follow-up questions.</p>
              </>
            )}
          </div>
        )}
        {turns.map((turn) => (
          <TurnView key={turn.key} turn={turn} onRate={(v) => rate(turn, v)} />
        ))}
        <div ref={end} />
      </div>

      <form className="ask" onSubmit={ask}>
        <label htmlFor="question" className="visually-hidden">Your question</label>
        <input id="question" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={`Ask about ${workspace.name}`} maxLength={2000} autoComplete="off" />
        <button className="primary" disabled={busy || !question.trim()}>Ask</button>
      </form>
    </div>
  );
}

export function TurnView({ turn, onRate }: { turn: Turn; onRate?: (value: -1 | 1) => void }) {
  const [active, setActive] = useState<number | null>(null);
  const cited = citedNumbers(turn.answer ?? "");
  const sources = turn.sources ?? [];
  const shown = sources.filter((s) => cited.has(s.n));
  const rest = sources.filter((s) => !cited.has(s.n));
  const rewritten = turn.searchedFor && turn.searchedFor.trim().toLowerCase() !== turn.question.trim().toLowerCase();

  return (
    <article className="turn">
      <div className="answer">
        <h2>{turn.question}</h2>
        {rewritten && <p className="searched">Searched for: {turn.searchedFor}</p>}
        {turn.answer !== undefined && (
          <p className="prose" aria-busy={turn.streaming || undefined}>
            {turn.answer.split(TOKENS).map((part, i) => {
              const m = part.match(CITATION);
              if (m && m[0] === part)
                return m[1].split(",").map((n) => (
                  <button key={`${i}-${n}`} className="cite" aria-label={`Show source ${n.trim()}`} onClick={() => setActive(Number(n.trim()))}>
                    {n.trim()}
                  </button>
                ));
              if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
              if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
              return part;
            })}
          </p>
        )}
        {turn.answer === undefined && !turn.error && (
          <p className="pending" role="status">{turn.sources ? "Writing the answer…" : "Reading the documents…"}</p>
        )}
        {turn.error && <p className="error" role="alert">{turn.error}</p>}
        {turn.messageId !== undefined && onRate && (
          <div className="rate" role="group" aria-label="Was this answer helpful?">
            <button aria-pressed={turn.feedback === 1} onClick={() => onRate(1)}>Helpful</button>
            <button aria-pressed={turn.feedback === -1} onClick={() => onRate(-1)}>Not helpful</button>
          </div>
        )}
      </div>
      {sources.length > 0 && !turn.streaming && (
        <aside className="notes" aria-label="Sources">
          <ol>
            {shown.map((s) => (
              <Note key={s.n} source={s} open={active === s.n} />
            ))}
          </ol>
          {rest.length > 0 && (
            <details className="uncited">
              <summary>
                {shown.length ? `${rest.length} more found, not cited` : `${rest.length} passages found, none cited`}
              </summary>
              <ol>
                {rest.map((s) => (
                  <Note key={s.n} source={s} open={active === s.n} />
                ))}
              </ol>
            </details>
          )}
        </aside>
      )}
    </article>
  );
}

function Note({ source, open }: { source: Source; open: boolean }) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (open) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open]);
  const file = source.document.split("/").pop();
  return (
    <li ref={ref} className={open ? "note active" : "note"}>
      <details open={open || undefined}>
        <summary>
          <span className="n">{source.n}</span>
          <span className="file">{file}</span>
          <span className="where">
            {source.heading || source.document}
            {source.page ? `, page ${source.page}` : ""}
          </span>
        </summary>
        {file !== source.document && <p className="path">{source.document}</p>}
        <pre>{source.text}</pre>
      </details>
    </li>
  );
}
