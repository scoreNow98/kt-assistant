"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, post, type Source, type Workspace } from "./api";

type Turn = { id: number; question: string; answer?: string; sources?: Source[]; error?: string };
type Doc = { name: string; chunks: number };

const CITATION = /\[(\d+(?:\s*,\s*\d+)*)\]/;
const TOKENS = /(\[\d+(?:\s*,\s*\d+)*\]|`[^`\n]+`|\*\*[^*\n]+\*\*)/;

const citedNumbers = (answer: string) =>
  new Set([...answer.matchAll(new RegExp(CITATION, "g"))].flatMap((m) => m[1].split(",").map((n) => Number(n.trim()))));

export default function Chat({ workspace }: { workspace: Workspace }) {
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
  }, [loadDocs]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const text = question.trim();
    if (!text || busy) return;
    const id = Date.now();
    setTurns((t) => [...t, { id, question: text }]);
    setQuestion("");
    setBusy(true);
    let patch: Partial<Turn>;
    try {
      patch = await api<{ answer: string; sources: Source[] }>(`/workspaces/${workspace.id}/ask`, post({ question: text }));
    } catch (err) {
      patch = { error: err instanceof Error ? err.message : "Something went wrong" };
    }
    setTurns((t) => t.map((turn) => (turn.id === id ? { ...turn, ...patch } : turn)));
    setBusy(false);
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
                <p>Answers come only from this workspace's documents, with the source of each statement beside it.</p>
              </>
            )}
          </div>
        )}
        {turns.map((turn) => (
          <TurnView key={turn.id} turn={turn} />
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

function TurnView({ turn }: { turn: Turn }) {
  const [active, setActive] = useState<number | null>(null);
  const cited = citedNumbers(turn.answer ?? "");
  const sources = turn.sources ?? [];
  const shown = sources.filter((s) => cited.has(s.n));
  const rest = sources.filter((s) => !cited.has(s.n));

  return (
    <article className="turn">
      <div className="answer">
        <h2>{turn.question}</h2>
        {turn.error ? (
          <p className="error" role="alert">{turn.error}</p>
        ) : turn.answer === undefined ? (
          <p className="pending" role="status">Reading the documents…</p>
        ) : (
          <p className="prose">
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
      </div>
      {sources.length > 0 && (
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
