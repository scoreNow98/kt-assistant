const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  /** body is the parsed error response, for example {detail, can_replace} on a 409 upload. */
  constructor(public status: number, message: string, public body: Record<string, unknown> = {}) {
    super(message);
  }
}

/** Every workspace endpoint answers 404 when the caller lost access (or the workspace was deleted). */
export const lostAccess = (e: unknown) => e instanceof ApiError && e.status === 404;

export const errorText = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** Calls the API with the session cookie. Throws ApiError with a message fit to show the user. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const json = typeof init.body === "string";
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      ...init,
      credentials: "include",
      headers: json ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check that it is running, then try again.");
  }
  if (!res.ok) {
    signOutIfExpired(res, path);
    throw await errorFrom(res);
  }
  return (await res.json().catch(() => ({}))) as T;
}

/** A session that expired while the page was open: reload, which shows the sign-in page. */
function signOutIfExpired(res: Response, path: string) {
  if (res.status === 401 && path !== "/me" && !path.startsWith("/auth/")) window.location.reload();
}

async function errorFrom(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  const detail = typeof body.detail === "string" ? body.detail : null;
  return new ApiError(
    res.status,
    detail ?? (res.status === 422 ? "Check what you entered and try again." : `The server answered with an error (${res.status}).`),
    body && typeof body === "object" ? body : {},
  );
}

/** POSTs JSON and calls onLine for each line of newline-separated JSON the server streams back.
 *  When signal aborts, the browser's AbortError is rethrown as is, so the caller can tell a stop from a failure. */
export async function stream(path: string, body: unknown, onLine: (line: Record<string, unknown>) => void, signal?: AbortSignal) {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  } catch (e) {
    if (signal?.aborted) throw e;
    throw new ApiError(0, "Cannot reach the server. Check that it is running, then try again.");
  }
  if (!res.ok || !res.body) {
    signOutIfExpired(res, path);
    throw await errorFrom(res);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      if (signal?.aborted) throw e;
      throw new ApiError(0, "The connection closed before the answer finished. Try again.");
    }
    const { done, value } = chunk;
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const parsed = JSON.parse(line);
      if ("done" in parsed || "error" in parsed) finished = true;
      onLine(parsed);
    }
    if (done) break;
  }
  if (!finished) throw new ApiError(0, "The connection closed before the answer finished. Try again.");
}

export const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export type Workspace = { id: number; name: string };
export type Me = { email: string; role: "admin" | "member"; org: string; workspaces: Workspace[] };
export type Source = { n: number; document: string; heading: string; page: number | null; text: string; distance?: number | null };
export type Health = { api: string; db: string; pgvector: string | null };
export type Message = { id: number; question: string; searched_for: string; answer: string; sources: Source[]; feedback: -1 | 1 | null; not_found: boolean; created_at: string };
export type Doc = { id: number; name: string; chunks: number; created_at: string; yours: boolean };

/** "12 Nov" style date for lists. */
export const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
