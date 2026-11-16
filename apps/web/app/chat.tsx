"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, day, errorText, lostAccess, stream, type Doc, type Message, type Source, type Workspace } from "./api";

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
  notFound?: boolean;
  stopped?: boolean; // the person pressed Stop; the server does not save such an answer
  earlier?: boolean; // loaded from the server: shown, but never sent as follow-up context
  topic?: number; // turns of this visit with the current topic number can be follow-up context
  finishedAt?: number; // Date.now() when the answer finished
  removeError?: string;
};
type UploadResult = { name: string; outcome: string; failed?: boolean };

// [1], [1, 3] and [Source 1]; keep in step with CITED in apps/api/rag.py.
const CITATION = /\[(?:Sources? )?(\d+(?:\s*,\s*\d+)*)\]/;
const TOKENS = /(\[(?:Sources? )?\d+(?:\s*,\s*\d+)*\]|`[^`\n]+`|\*\*[^*\n]+\*\*)/;

const ALLOWED = [".md", ".mdx", ".txt", ".pdf", ".docx"]; // keep in step with SUFFIXES in apps/api/rag.py
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024; // keep in step with MAX_UPLOAD_BYTES in apps/api/main.py
const FOLLOW_UP_MS = 30 * 60 * 1000; // an answer older than this is not used as follow-up context

const citedNumbers = (answer: string) =>
  new Set([...answer.matchAll(new RegExp(CITATION, "g"))].flatMap((m) => m[1].split(",").map((n) => Number(n.trim()))));

const shorten = (text: string, max = 60) => (text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text);

const fromMessage = (m: Message): Turn => ({
  key: `m${m.id}`,
  question: m.question,
  searchedFor: m.searched_for,
  answer: m.answer,
  sources: m.sources,
  messageId: m.id,
  feedback: m.feedback,
  notFound: m.not_found,
  earlier: true,
});

/** The turns sent as follow-up context: saved answers of this visit in the current topic, if the last one is recent. */
function followUpContext(turns: Turn[], topic: number, now: number): Turn[] {
  const answered = turns.filter((t) => !t.earlier && t.topic === topic && t.messageId !== undefined && !t.streaming);
  const last = answered[answered.length - 1];
  if (!last?.finishedAt || now - last.finishedAt >= FOLLOW_UP_MS) return [];
  return answered.slice(-2);
}

const draftKey = (workspaceId: number) => `kt-draft-${workspaceId}`;
function readDraft(workspaceId: number) {
  try {
    return typeof window === "undefined" ? "" : (window.sessionStorage.getItem(draftKey(workspaceId)) ?? "");
  } catch {
    return ""; // storage can be blocked; a lost draft is not worth an error
  }
}

export default function Chat({
  workspace,
  nav,
  title,
  isAdmin,
  active,
  onLostAccess,
  onHandover,
}: {
  workspace: Workspace;
  nav: React.ReactNode;
  title: React.ReactNode;
  isAdmin: boolean;
  active: boolean;
  onLostAccess: () => void;
  onHandover: () => void;
}) {
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState(() => readDraft(workspace.id));
  const [busy, setBusy] = useState(false);
  const [topic, setTopic] = useState(0);
  const [status, setStatus] = useState(""); // upload progress and finished actions
  const [problem, setProblem] = useState(""); // a failed header action
  const [results, setResults] = useState<UploadResult[]>([]);
  const [filter, setFilter] = useState("");
  const [, setTick] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const stopper = useRef<AbortController | null>(null);

  const loadDocs = useCallback(
    () =>
      api<Doc[]>(`/workspaces/${workspace.id}/documents`)
        .then(setDocs)
        .catch((e) => {
          if (lostAccess(e)) onLostAccess();
          setDocs([]);
        }),
    [workspace.id, onLostAccess],
  );
  useEffect(() => {
    if (active) loadDocs(); // also after a handover note was saved on another tab
  }, [active, loadDocs]);
  useEffect(() => {
    api<Message[]>(`/workspaces/${workspace.id}/messages`)
      .then((ms) =>
        setTurns((now) => {
          const seen = new Set(now.map((t) => t.messageId));
          return [...ms.filter((m) => !seen.has(m.id)).map(fromMessage), ...now]; // keep a question asked while this loaded
        }),
      )
      .catch((e) => {
        if (lostAccess(e)) onLostAccess();
      });
  }, [workspace.id, onLostAccess]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [turns.length]);
  useEffect(() => () => stopper.current?.abort(), []); // leaving the workspace stops the answer
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), 60_000); // the 30-minute follow-up limit passes without a click
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    try {
      if (question) window.sessionStorage.setItem(draftKey(workspace.id), question);
      else window.sessionStorage.removeItem(draftKey(workspace.id));
    } catch {
      // storage can be blocked; the draft then lives only in this page
    }
  }, [question, workspace.id]);
  useEffect(() => {
    const el = input.current;
    if (!el || !active) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`; // CSS max-height stops it at 6 lines
  }, [question, active]);

  const patch = (key: string, change: (t: Turn) => Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.key === key ? { ...t, ...change(t) } : t)));

  async function ask(raw: string) {
    const text = raw.trim();
    if (!text || busy) return;
    // The last two answered turns let the server understand a follow-up such as "and how do I restart it?".
    const history = followUpContext(turns, topic, Date.now()).map((t) => ({ question: t.question, answer: t.answer?.slice(0, 600) })); // the server reads no more
    const key = `q${Date.now()}`;
    const controller = new AbortController();
    stopper.current = controller;
    setTurns((all) => [...all, { key, question: text, streaming: true, topic }]);
    setBusy(true);
    try {
      await stream(
        `/workspaces/${workspace.id}/ask`,
        { question: text, history },
        (line) => {
          if ("sources" in line) patch(key, () => ({ searchedFor: line.searched_for as string, sources: line.sources as Source[] }));
          else if ("delta" in line) patch(key, (t) => ({ answer: (t.answer ?? "") + (line.delta as string) }));
          else if ("error" in line) patch(key, () => ({ error: line.error as string }));
          else if ("done" in line)
            patch(key, () => ({ messageId: line.message_id as number, feedback: null, notFound: line.not_found === true, finishedAt: Date.now() }));
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) patch(key, (t) => (t.messageId === undefined ? { stopped: true } : {}));
      else {
        if (lostAccess(err)) onLostAccess();
        patch(key, () => ({ error: errorText(err, "Something went wrong") }));
      }
    }
    patch(key, () => ({ streaming: false }));
    stopper.current = null;
    setBusy(false);
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !question.trim()) return;
    const text = question;
    setQuestion("");
    ask(text);
  }

  function newTopic() {
    setTopic((t) => t + 1);
    input.current?.focus();
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

  async function removeTurn(turn: Turn) {
    if (!window.confirm(`Remove "${shorten(turn.question)}" and its answer? This cannot be undone.`)) return;
    try {
      await api(`/messages/${turn.messageId}`, { method: "DELETE" });
    } catch (err) {
      // 404: it is already gone, so it leaves the list either way.
      if (!(err instanceof ApiError && err.status === 404)) {
        patch(turn.key, () => ({ removeError: errorText(err, "Could not remove this question. Try again.") }));
        return;
      }
    }
    setTurns((all) => all.filter((t) => t.key !== turn.key));
  }

  async function clearMine() {
    if (!window.confirm(`Remove all of your questions and answers in ${workspace.name}? Other people's questions stay. This cannot be undone.`)) return;
    setProblem("");
    try {
      const r = await api<{ deleted?: number }>(`/workspaces/${workspace.id}/messages`, { method: "DELETE" });
      const n = r.deleted ?? 0;
      setTurns([]);
      setStatus(`Removed ${n} question${n === 1 ? "" : "s"}.`);
    } catch (err) {
      if (lostAccess(err)) onLostAccess();
      else setProblem(errorText(err, "Could not remove your questions. Try again."));
    }
  }

  async function removeDoc(doc: Doc) {
    if (!window.confirm(`Remove ${doc.name} from ${workspace.name}? Answers will no longer use it.`)) return;
    setProblem("");
    try {
      await api(`/workspaces/${workspace.id}/documents/${doc.id}`, { method: "DELETE" });
      setStatus(`${doc.name} removed.`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setStatus(`${doc.name} was already removed.`);
      else setProblem(errorText(err, "Could not remove the document. Try again."));
    }
    loadDocs(); // a lost workspace shows up here as a 404 on the list
  }

  /** Sends one file. Returns null when the workspace is no longer open to this person. */
  async function send(file: File, replace = false): Promise<UploadResult | null> {
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await api<{ name?: string; status?: string }>(`/workspaces/${workspace.id}/documents${replace ? "?replace=true" : ""}`, {
        method: "POST",
        body: form,
      });
      const name = r.name || file.name;
      if (r.status === "duplicate") return { name, outcome: "already here with the same content, so nothing changed" };
      if (r.status === "replaced") return { name, outcome: "replaced the earlier version" };
      return { name, outcome: "added" };
    } catch (err) {
      if (lostAccess(err)) return null;
      if (err instanceof ApiError && err.status === 409 && !replace) {
        if (err.body.can_replace === true) {
          if (window.confirm(`A document named ${file.name} is already here. Replace it?`)) return send(file, true);
          return { name: file.name, outcome: "not added. You kept the document that was already here.", failed: true };
        }
        return {
          name: file.name,
          outcome: "not added. A different document with this name is already here. An admin or the person who added it can replace it.",
          failed: true,
        };
      }
      return { name: file.name, outcome: `could not be read: ${errorText(err, "the server did not say why")}`, failed: true };
    }
  }

  async function addFiles(list: FileList) {
    const files = Array.from(list);
    if (fileInput.current) fileInput.current.value = "";
    setProblem("");
    const out: UploadResult[] = [];
    const ready: File[] = [];
    for (const f of files) {
      const dot = f.name.lastIndexOf(".");
      const ext = dot > 0 ? f.name.slice(dot).toLowerCase() : "";
      if (!ALLOWED.includes(ext))
        out.push({ name: f.name, outcome: `not added. Only Markdown, text, PDF and Word files can be read (${ALLOWED.join(", ")}).`, failed: true });
      else if (f.size === 0) out.push({ name: f.name, outcome: "not added. This file is empty.", failed: true });
      else if (f.size > MAX_UPLOAD_BYTES) out.push({ name: f.name, outcome: "not added. This file is larger than 20 MB.", failed: true });
      else ready.push(f);
    }
    setResults([...out]);
    for (let i = 0; i < ready.length; i++) {
      setStatus(`Reading ${i + 1} of ${ready.length}: ${ready[i].name}`);
      const r = await send(ready[i]);
      if (!r) {
        setStatus("");
        onLostAccess();
        return;
      }
      out.push(r);
      setResults([...out]);
    }
    setStatus(`Finished with ${files.length} file${files.length === 1 ? "" : "s"}. The list below says what happened to each.`);
    loadDocs();
  }

  const now = Date.now();
  const previous = followUpContext(turns, topic, now).slice(-1)[0];
  const local = turns.filter((t) => !t.earlier);
  const freshTopic = !previous && topic > 0 && local[local.length - 1]?.topic !== topic;
  const needle = filter.trim().toLowerCase();
  const listed = docs?.filter((d) => !needle || d.name.toLowerCase().includes(needle)) ?? [];

  return (
    <div className="chat">
      <header>
        {title}
        <details className="docs">
          <summary>{docs === null ? "Loading documents…" : `${docs.length} document${docs.length === 1 ? "" : "s"}`}</summary>
          <div className="docs-menu">
            {docs && docs.length > 10 && (
              <label className="docs-filter">
                Filter by name
                <input type="search" value={filter} onChange={(e) => setFilter(e.target.value)} />
              </label>
            )}
            <ul>
              {listed.map((d) => (
                <li key={d.id}>
                  <span className="name">{d.name}</span>
                  {d.created_at && <span className="added">added {day(d.created_at)}</span>}
                  {(isAdmin || d.yours) && (
                    <button className="link" aria-label={`Remove ${d.name}`} onClick={() => removeDoc(d)}>
                      Remove
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {needle && listed.length === 0 && <p className="quiet">No document name contains "{filter.trim()}".</p>}
          </div>
        </details>
        <button onClick={() => fileInput.current?.click()}>Add documents</button>
        {turns.some((t) => t.messageId !== undefined) && (
          <button className="link" disabled={busy} onClick={clearMine}>
            Clear my questions
          </button>
        )}
        <input ref={fileInput} type="file" hidden multiple accept={ALLOWED.join(",")} onChange={(e) => e.target.files?.length && addFiles(e.target.files)} />
        {nav}
        <p className="upload-status" role="status">{status}</p>
        {problem && <p className="upload-status error" role="alert">{problem}</p>}
        {results.length > 0 && (
          <div className="upload-results">
            <ul aria-label="Files you added">
              {results.map((r, i) => (
                <li key={`${i}-${r.name}`} className={r.failed ? "failed" : undefined}>
                  <span className="name">{r.name}</span> {r.outcome}
                </li>
              ))}
            </ul>
            <button
              className="link"
              onClick={() => {
                setResults([]);
                setStatus("");
              }}
            >
              Dismiss
            </button>
          </div>
        )}
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
        {turns.map((turn, i) => {
          const before = turns[i - 1];
          const after = turns[i + 1];
          return (
            <Fragment key={turn.key}>
              {before && !before.earlier && !turn.earlier && before.topic !== turn.topic && <p className="divider">New topic</p>}
              <TurnView
                turn={turn}
                busy={busy}
                onRate={(v) => rate(turn, v)}
                onRemove={() => removeTurn(turn)}
                onRetry={() => ask(turn.question)}
                onEdit={() => {
                  setQuestion(turn.question);
                  input.current?.focus();
                }}
                onHandover={onHandover}
              />
              {turn.earlier && !after?.earlier && <p className="divider">Earlier questions end here. New questions start fresh.</p>}
            </Fragment>
          );
        })}
        <div ref={end} />
      </div>

      <form className="ask" onSubmit={submit}>
        {previous && (
          <p className="follow-up">
            Follow-up to "{shorten(previous.question)}"{" "}
            <button type="button" className="link" onClick={newTopic}>
              Ask as a new question
            </button>
          </p>
        )}
        {freshTopic && <p className="follow-up">New topic. Your next question will not use earlier answers.</p>}
        <label htmlFor="question" className="visually-hidden">Your question</label>
        <textarea
          id="question"
          ref={input}
          rows={1}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={`Ask about ${workspace.name}`}
          maxLength={2000}
          autoComplete="off"
          aria-describedby="question-keys"
        />
        <span id="question-keys" className="visually-hidden">Enter sends. Shift and Enter starts a new line.</span>
        <button
          className="primary"
          type={busy ? "button" : "submit"}
          onClick={busy ? () => stopper.current?.abort() : undefined}
          disabled={!busy && !question.trim()}
        >
          {busy ? "Stop" : "Ask"}
        </button>
        {turns.length > 0 && (
          <button type="button" onClick={newTopic}>
            New topic
          </button>
        )}
      </form>
    </div>
  );
}

export function TurnView({
  turn,
  busy,
  onRate,
  onRemove,
  onRetry,
  onEdit,
  onHandover,
}: {
  turn: Turn;
  busy?: boolean;
  onRate?: (value: -1 | 1) => void;
  onRemove?: () => void;
  onRetry?: () => void;
  onEdit?: () => void;
  onHandover?: () => void;
}) {
  const [active, setActive] = useState<number | null>(null);
  const cited = citedNumbers(turn.answer ?? "");
  const sources = turn.sources ?? [];
  const shown = sources.filter((s) => cited.has(s.n));
  const rest = sources.filter((s) => !cited.has(s.n));
  const rewritten = turn.searchedFor && turn.searchedFor.trim().toLowerCase() !== turn.question.trim().toLowerCase();
  const notFound = turn.notFound && !turn.streaming;
  const saved = turn.messageId !== undefined;

  return (
    <article className={notFound ? "turn not-found" : "turn"}>
      <div className="answer">
        <h2>{turn.question}</h2>
        {rewritten && <p className="searched">Searched for: {turn.searchedFor}</p>}
        {turn.answer !== undefined && (
          <p className="prose" aria-busy={turn.streaming || undefined}>
            {turn.answer.split(TOKENS).map((part, i) => {
              const m = part.match(CITATION);
              if (m && m[0] === part)
                return m[1].split(",").map((raw) => {
                  const n = Number(raw.trim());
                  // A number the model made up, with no source behind it, is not shown as a citation.
                  if (!sources.some((s) => s.n === n)) return <span key={`${i}-${n}`} className="bad-cite">[{n}]</span>;
                  return (
                    <button key={`${i}-${n}`} className="cite" aria-label={`Show source ${n}`} onClick={() => setActive(n)}>
                      {n}
                    </button>
                  );
                });
              if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
              if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
              return part;
            })}
          </p>
        )}
        {turn.answer === undefined && !turn.error && !turn.stopped && (
          <p className="pending" role="status">{turn.sources ? "Writing the answer…" : "Reading the documents…"}</p>
        )}
        {turn.stopped && <p className="stopped" role="status">Stopped. This answer was not saved.</p>}
        {turn.error && <p className="error" role="alert">{turn.error}</p>}
        {notFound && onHandover && (
          <p className="handover-hint">
            Know the answer? <button className="link" onClick={onHandover}>Add it under Hand over</button>
          </p>
        )}
        {(turn.stopped || turn.error) && !saved && onRetry && (
          <div className="turn-actions">
            <button disabled={busy} onClick={onRetry}>Try again</button>
            {onEdit && <button onClick={onEdit}>Edit</button>}
          </div>
        )}
        {saved && (onRate || onRemove) && (
          <div className="rate">
            {onRate && !notFound && (
              <div className="row" role="group" aria-label="Was this answer helpful?">
                <button aria-pressed={turn.feedback === 1} onClick={() => onRate(1)}>Helpful</button>
                <button aria-pressed={turn.feedback === -1} onClick={() => onRate(-1)}>Not helpful</button>
              </div>
            )}
            {onRemove && (
              <button className="link remove" aria-label={`Remove the question "${shorten(turn.question)}"`} onClick={onRemove}>
                Remove
              </button>
            )}
          </div>
        )}
        {turn.removeError && <p className="error" role="alert">{turn.removeError}</p>}
      </div>
      {sources.length > 0 && !turn.streaming && (
        <aside className="notes" aria-label="Sources">
          {notFound ? (
            <details className="uncited">
              <summary>Closest passages</summary>
              <ol>
                {sources.map((s) => (
                  <Note key={s.n} source={s} open={active === s.n} />
                ))}
              </ol>
            </details>
          ) : (
            <>
              <ol>
                {shown.map((s) => (
                  <Note key={s.n} source={s} open={active === s.n} />
                ))}
              </ol>
              {rest.length > 0 && (
                <details className="uncited">
                  <summary>
                    {shown.length ? `${rest.length} more found, not cited` : turn.stopped ? `${rest.length} passages found` : `${rest.length} passages found, none cited`}
                  </summary>
                  <ol>
                    {rest.map((s) => (
                      <Note key={s.n} source={s} open={active === s.n} />
                    ))}
                  </ol>
                </details>
              )}
            </>
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
