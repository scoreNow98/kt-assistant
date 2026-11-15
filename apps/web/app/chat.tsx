"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Source = { n: number; document: string; heading: string; page: number | null; text: string };
type Answer = { answer: string; sources: Source[] };

async function call(path: string, init: RequestInit) {
  const res = await fetch(`${API}${path}`, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`);
  return body;
}

export default function Chat() {
  const [workspace, setWorkspace] = useState("client-a");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(label: string, work: () => Promise<void>) {
    setBusy(true);
    setStatus(label);
    try {
      await work();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  const upload = (file: File) =>
    run(`Reading ${file.name}…`, async () => {
      const form = new FormData();
      form.append("file", file);
      const r = await call(`/workspaces/${workspace}/documents`, { method: "POST", body: form });
      setStatus(r.status === "duplicate" ? `${r.name} is already in ${workspace}` : `${r.name}: ${r.chunks} chunks stored`);
    });

  const ask = () =>
    run("Thinking…", async () => {
      setAnswer(
        await call(`/workspaces/${workspace}/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
        }),
      );
      setStatus("");
    });

  return (
    <section style={{ maxWidth: 760 }}>
      <p>
        <label>
          Workspace{" "}
          <input value={workspace} onChange={(e) => setWorkspace(e.target.value)} pattern="[a-z0-9-]+" />
        </label>{" "}
        <label>
          Add a document{" "}
          <input
            type="file"
            accept=".md,.mdx,.txt,.pdf,.docx"
            disabled={busy}
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
        </label>
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) ask();
        }}
      >
        <label>
          Question{" "}
          <input value={question} onChange={(e) => setQuestion(e.target.value)} style={{ width: "70%" }} />
        </label>{" "}
        <button disabled={busy}>Ask</button>
      </form>
      <p role="status">{status}</p>
      {answer && (
        <>
          <p style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</p>
          <ol>
            {answer.sources.map((s) => (
              <li key={s.n}>
                <details>
                  <summary>
                    {s.document}
                    {s.heading && ` › ${s.heading}`}
                    {s.page && ` (page ${s.page})`}
                  </summary>
                  <pre style={{ whiteSpace: "pre-wrap" }}>{s.text}</pre>
                </details>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
