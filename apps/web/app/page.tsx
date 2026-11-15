import Chat from "./chat";

// The health line is rendered on the server; the chat calls the API from the browser.
export const dynamic = "force-dynamic";

type Health = { api: string; db: string; pgvector: string | null };

async function getHealth(): Promise<Health> {
  try {
    const res = await fetch(`${process.env.API_URL}/health`, { cache: "no-store" });
    return await res.json();
  } catch {
    return { api: "down", db: "unknown", pgvector: null };
  }
}

export default async function Home() {
  const h = await getHealth();
  return (
    <main>
      <h1>KT Assistant</h1>
      <p>
        API: {h.api} · Database: {h.db} · pgvector: {h.pgvector ?? "not installed"}
      </p>
      <Chat />
    </main>
  );
}
