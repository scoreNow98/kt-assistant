import App from "./app";
import type { Health } from "./api";

// The health check runs on the server; everything else calls the API from the browser.
export const dynamic = "force-dynamic";

async function getHealth(): Promise<Health> {
  try {
    const res = await fetch(`${process.env.API_URL}/health`, { cache: "no-store" });
    return await res.json();
  } catch {
    return { api: "down", db: "unknown", pgvector: null };
  }
}

export default async function Home() {
  return <App health={await getHealth()} />;
}
