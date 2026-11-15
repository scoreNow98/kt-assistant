# KT Assistant

Knowledge-transfer assistant for service teams: one workspace per client, answers with links to the source document.

Status: **phase 4, knowledge transfer**. Each client workspace has four tabs:

- **Ask:** questions answered from the documents, with sources beside each answer (phase 3).
- **Start here:** an onboarding guide an admin creates in one click: five cited sections and a reading order.
- **Gaps:** questions from the last 30 days the documents could not answer, or whose answers were marked not helpful. Who asked is not shown.
- **Hand over:** someone leaving answers those gaps and a few standard questions; the answers are saved as a document that later answers cite.

## One-time setup

1. Install and open [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Install [Ollama](https://ollama.com/download), open it, then download the two free models (about 4.5 GB):

```bash
ollama pull bge-m3      # embeddings, multilingual
ollama pull gemma3:4b   # answers
```

3. Download the sample documents. Two open-source projects stand in for two clients; no company data is used.

```bash
./scripts/fetch_sample_data.sh   # FastAPI docs -> client-a, Ollama docs -> client-b
```

## Run

```bash
docker compose up --build
```

Open http://localhost:3000 and choose "Create a company account". The first account of a company is its admin.

Load the sample documents into two workspaces of your company (in a second terminal, several minutes the first time). Use the email you signed up with:

```bash
docker compose exec api python ingest.py you@example.com client-a /sample-data/client-a
docker compose exec api python ingest.py you@example.com client-b /sample-data/client-b
```

Reload the page, open `client-a` and ask, for example, "How do I allow requests from a frontend on a different origin?".

## Who can see what

- An **admin** sees every workspace of the company, creates workspaces and invites people under "Team and access".
- A **member** sees only the workspaces an admin ticked for them.
- Another company sees nothing. This is enforced twice: in the API, and in Postgres itself with row-level security (`apps/api/schema.sql`).

Invites are links the admin copies and sends; the app does not send email.

## Check

```bash
docker compose exec api pytest                           # includes the cross-company leak tests
docker compose exec api python eval.py you@example.com   # do 30 test questions find the right document?
```

## Change the models

The API talks to any OpenAI-compatible server. Create a `.env` file next to `docker-compose.yml`:

```bash
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_API_KEY=ollama
EMBED_MODEL=bge-m3
CHAT_MODEL=gemma3:4b
```

Documents embedded with one embedding model are not searched with another, so reload the documents after changing `EMBED_MODEL`.

## Layout

```
apps/api/     FastAPI backend: main.py routes, auth.py logins, db.py connections, rag.py search and answers
apps/web/     Next.js frontend
infra/        database setup
scripts/      helper scripts
```

`eval.py` prints two scores: vector search alone, and the hybrid search the app uses. The last 10 questions are harder on purpose: exact setting names and loosely worded questions.

## Next: phase 5

Make it sellable: usage limits per company, billing, a landing page, cloud deployment, error alerts and backups.
