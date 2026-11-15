# KT Assistant

Knowledge-transfer assistant for service teams: one workspace per client, answers with links to the source document.

Status: **phase 0, skeleton**. The stack starts and the health page shows the API, the database and pgvector. No RAG yet.

## Run

```bash
docker compose up --build
```

- Web: http://localhost:3000 (health page)
- API: http://localhost:8000/health and http://localhost:8000/docs

## Sample data

No company data is used. Two open-source projects stand in for two clients:

```bash
./scripts/fetch_sample_data.sh   # FastAPI docs -> client-a, Ollama docs -> client-b
```

## Test

```bash
cd apps/api && pip install -r requirements.txt && pytest
```

## Layout

```
apps/api/     FastAPI backend
apps/web/     Next.js frontend
infra/        database setup
scripts/      helper scripts
```

## Next: phase 1

Upload, parse, chunk, embed, vector search and a cited answer for one user.
