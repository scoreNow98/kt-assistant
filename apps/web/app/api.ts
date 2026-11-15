const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

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
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : null;
    throw new ApiError(res.status, detail ?? (res.status === 422 ? "Check what you entered and try again." : `The server answered with an error (${res.status}).`));
  }
  return body as T;
}

export const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export type Workspace = { id: number; name: string };
export type Me = { email: string; role: "admin" | "member"; org: string; workspaces: Workspace[] };
export type Source = { n: number; document: string; heading: string; page: number | null; text: string };
export type Health = { api: string; db: string; pgvector: string | null };
