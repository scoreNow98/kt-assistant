# KT Assistant

Knowledge-transfer assistant for service teams: one workspace per client, answers with links to the source document.

Status: **phase 5, ready for a first paying pilot** (billing not built yet). Each client workspace has four tabs:

- **Ask:** questions answered from the documents, with sources beside each answer (phase 3). A follow-up only counts within the same visit and 30 minutes, and the page shows when a question is being treated as a follow-up. An answer can be stopped, retried, or removed.
- **Start here:** an onboarding guide an admin creates in one click: five cited sections and a reading order.
- **Gaps:** questions from the last 30 days the documents could not answer, or whose answers were marked not helpful. Who asked is not shown. A gap closes by itself once the question is asked again and answered. Admins can dismiss junk questions.
- **Hand over:** someone leaving answers those gaps and a few standard questions; the answers are saved as a document that later answers cite.

## One-time setup

1. Install and open [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Install [Ollama](https://ollama.com/download), open it, then download the two free models (about 4.5 GB):

```bash
ollama pull bge-m3      # embeddings, multilingual
ollama pull gemma3:4b   # answers
```

   Then give Ollama room for the sources sent with each question (its default is about 4k tokens, which cuts them off), and quit and reopen the Ollama app:

```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 8192
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

After an update that changes how documents are split, load them again with `--replace`:

```bash
docker compose exec api python ingest.py --replace you@example.com client-a /sample-data/client-a
```

Reload the page, open `client-a` and ask, for example, "How do I allow requests from a frontend on a different origin?".

## Who can see what

- An **admin** sees every workspace of the company, creates workspaces and invites people under "Team and access".
- A **member** sees only the workspaces an admin ticked for them.
- Another company sees nothing. This is enforced twice: in the API, and in Postgres itself with row-level security (`apps/api/schema.sql`).

Invites are links the admin copies and sends; the app does not send email. An admin, or the person who added it, can remove a document (in the document list of a workspace). Admins can remove a person (under "Team and access"); a removed person is signed out at once.

## Fixing mistakes

- **Wrong document:** the person who added it or an admin removes it from the document list of the workspace. Uploading a changed file with the same name asks whether to replace the old one.
- **Wrong file picked:** files of the wrong type, empty files and files over 20 MB are refused before upload, with a reason per file.
- **Wrong workspace:** an admin can rename or delete it. Deleting removes its documents, questions and guide.
- **Wrong question (or something pasted by mistake):** each person can remove their own question, or all of them, from a workspace. The text is erased; the question still counts toward the month's plan usage.
- **Wrong invite or role:** pending invites can be revoked and a person's role changed under "Team and access".
- **Junk in the gap report:** an admin can dismiss it.

## Check

```bash
docker compose exec api pytest                           # includes the cross-company leak tests
docker compose exec api python eval.py you@example.com   # do the 30 answerable test questions find the right document?
docker compose exec api python eval_answers.py you@example.com   # are the answers themselves right?
```

`eval_answers.py` asks all 38 test questions with the real model. For the 30 answerable ones it checks that the answer contains a key fact, cites the right document and uses only real source numbers. For the 8 the documents cannot answer, it checks that the answer says so. It takes several minutes with a local model.

`eval.py` now prints how close the best match was for each question and suggests a value for `MAX_DISTANCE`. This is an optional cut-off (set in `.env`) that makes the assistant answer "not found" without calling the model when nothing in the documents is close enough.

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
apps/api/     FastAPI backend: main.py routes, auth.py logins, db.py connections, rag.py search and answers, admin.py operator commands
apps/web/     Next.js frontend
infra/        database setup and the production Caddy config
scripts/      helper scripts
```

`eval.py` prints two scores: vector search alone, and the hybrid search the app uses. The last 10 questions are harder on purpose: exact setting names and loosely worded questions.

## Plans and limits

Each company has a plan with a monthly question limit and limits on documents and people (`PLANS` in `apps/api/main.py`; the numbers are placeholders until pricing is set). Admins see usage under "Team and access". Until billing exists, change a company's plan by hand:

```bash
docker compose exec api python admin.py usage someone@company.com
docker compose exec api python admin.py set-plan someone@company.com team
```

## Deploy to a server

Any Linux server with Docker works, for example a small VM. Point a domain's DNS A record at it, then on the server:

```bash
git clone https://github.com/scoreNow98/kt-assistant.git && cd kt-assistant
cp .env.example .env   # fill in DOMAIN, the two passwords and the model API
docker compose -f docker-compose.prod.yml up -d --build
```

Caddy gets an HTTPS certificate for the domain by itself. The web app and the API share the domain; the API is under `/api`.

Models: a hosted OpenAI-compatible API is the easy choice. To keep everything free and on the server instead, add `--profile local-models` to run Ollama there (needs about 8 GB of memory free), set the model lines in `.env` as shown in `.env.example`, then pull the models once:

```bash
docker compose -f docker-compose.prod.yml exec ollama ollama pull bge-m3
docker compose -f docker-compose.prod.yml exec ollama ollama pull gemma3:4b
```

## Alerts, logs and backups

- **Alerts:** set `ALERT_WEBHOOK_URL` to a Slack, Google Chat or Discord incoming webhook. Server errors and model-server outages are posted there, at most once a minute.
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f api`
- **Backups:** `./scripts/backup.sh docker-compose.prod.yml` saves the database to `backups/` and keeps the newest 14. Run it daily with cron (the line is at the top of the script) and copy `backups/` off the server.
- **Restore:** `./scripts/restore.sh backups/<file>.sql.gz docker-compose.prod.yml` replaces all data with that backup after asking you to confirm.

## Known limits

From the review of phase 5 (security, correctness and accuracy), still open:

- Answers are grounded by the prompt only. The model always receives the best passages, even for a question the documents do not cover, so a small model can write a confident wrong answer. `MAX_DISTANCE` is the cut-off for this; it is off until tuned with `eval.py`.
- Answer quality can now be measured with `eval_answers.py`, but it has not been run with the real model yet, so the scores are unknown.
- An answer counts as "not found" when it says so or cites none of its sources; a confident wrong answer that cites a real source is still not caught.
- At most 8 answers are written at once per API process (`MAX_CONCURRENT_ANSWERS`); beyond that people see "busy, try again".
- Any company can sign up without verifying an email address, and signup reveals whether an email already has an account.
- Uploaded documents can contain instructions aimed at the model; they can mislead answers within that workspace.

## Next

Billing (needs a payment provider account), then connectors: GitHub repositories, then Slack and meeting notes.
