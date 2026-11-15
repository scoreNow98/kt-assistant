# KT Assistant

Knowledge-transfer assistant for service teams: one workspace per client, answers with links to the source document.

Status: **phase 1, core RAG for one user**. Upload documents into a workspace, ask a question, get an answer with numbered sources. No login yet.

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

Load the sample documents (in a second terminal, takes several minutes the first time):

```bash
docker compose exec api python ingest.py client-a /sample-data/client-a
docker compose exec api python ingest.py client-b /sample-data/client-b
```

Open http://localhost:3000, keep the workspace as `client-a`, and ask a question such as "How do I allow requests from a frontend on a different origin?". You can also add your own `.md`, `.txt`, `.pdf` or `.docx` file on that page.

## Check

```bash
docker compose exec api pytest          # unit tests plus a workspace-isolation test
docker compose exec api python eval.py  # do 20 test questions find the right document?
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
apps/api/     FastAPI backend (rag.py holds parse, chunk, embed, search, answer)
apps/web/     Next.js frontend
infra/        database setup
scripts/      helper scripts
```

## Next: phase 2

Login, organisations, client workspaces with members, and database-enforced isolation.
