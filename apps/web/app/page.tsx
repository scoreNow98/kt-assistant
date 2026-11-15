// Health page: rendered on the server, so the browser never calls the API directly.
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
      <ul>
        <li>API: {h.api}</li>
        <li>Database: {h.db}</li>
        <li>pgvector: {h.pgvector ?? "not installed"}</li>
      </ul>
    </main>
  );
}
