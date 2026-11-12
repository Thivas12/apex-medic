# ApexMedic

[Demo](https://apex-medic.onrender.com/ui/) · [Architecture](docs/architecture.md) · [Documentation](docs/README.md)

[![Tests](https://github.com/Thivas12/apex-medic/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Thivas12/apex-medic/actions/workflows/tests.yml)

ApexMedic helps Apex Legends players work through packet loss, high latency and
rubber-banding. It keeps a case history of readings, comparisons and outcomes,
then uses that evidence to choose the next question or test.

The backend is Python/FastAPI, with a plain JavaScript frontend. SQLite is used
for local development; the hosted app runs on Render with Neon PostgreSQL and
Groq. Ollama is also supported for local inference.

## Run locally

Use Python 3.12. The deployment patch version is pinned in [.python-version](.python-version).
No frontend build is needed.

```powershell
git clone https://github.com/Thivas12/apex-medic.git
cd apex-medic
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:APEX_DATABASE_BACKEND = "sqlite"
$env:APEX_LLM_PROVIDER = "ollama"
.\.venv\Scripts\python.exe -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend --no-proxy-headers
```

On Linux/macOS:

```bash
git clone https://github.com/Thivas12/apex-medic.git
cd apex-medic
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export APEX_DATABASE_BACKEND=sqlite
export APEX_LLM_PROVIDER=ollama
.venv/bin/python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend --no-proxy-headers
```

Open [the UI](http://127.0.0.1:8000/ui/) or
[API docs](http://127.0.0.1:8000/docs). The [demo walkthrough](docs/demo.md)
provides synthetic inputs for a complete investigation.

The case workflow works without a model service. To use **Explain this step**,
run Ollama with `qwen3:4b-instruct-2507-q4_K_M`, or configure
[Groq](docs/groq_setup.md). See [.env.example](.env.example) for settings.
The app reads `.env.local`; process environment variables take precedence.
Keep credentials in that ignored file or the hosting environment.

## How it works

1. A player creates a case and records symptoms and in-game readings.
2. [The policy](backend/app/agent/rules.py) selects one question, comparison or
   review step from the saved evidence.
3. [The knowledge layer](backend/app/knowledge.py) attaches reviewed guidance
   mapped to that action, with source references and a revision hash.
4. The optional model selects a supporting explanation from the allowed options.
5. New evidence is saved with an event ID and timestamp, and the policy runs again.

An early version asked the model to rewrite recommendations. Valid JSON still
contained changed conditions and unsupported causal claims. The current adapter
accepts an `option_id` and looks up the corresponding reviewed text on the
server. The recommended action is always included verbatim. The
[content review](docs/explanation_review.md) records why this changed.

Case updates, history and quota accounting commit together. Reusing an event ID
with the same payload is a safe retry; changing its payload is a conflict.
Browser sessions control case access. An explanation is discarded if the
recommendation changes while the model is running. See
[architecture](docs/architecture.md) for the request flow and storage tradeoffs.

## Tests

Node 24 is needed for the JavaScript tests; no npm packages are required.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check frontend/app.js
node --check frontend/manage.js
node --test tests/test_manage_page.cjs
```

The suite has 201 Python tests and 8 JavaScript tests. Local runs skip 21
PostgreSQL tests unless a disposable database is configured. CI runs them
against PostgreSQL and rejects skips. Provider responses are mocked; the
JavaScript tests use a mocked DOM. The [demo walkthrough](docs/demo.md) covers
manual browser checks.

Live model checks are separate from CI. The [evaluation notes](evaluation/README.md)
cover the two synthetic scenario suites, saved reports and content review.

## Scope

The current policy supports Windows PC connection issues and two comparisons:
nearby data centres and background downloads or streams. Players enter readings
and outcomes themselves. Reported improvement leads to verification and review;
it does not establish a root cause or automatically resolve the case.

Cases belong to an anonymous browser session that expires 30 days after
creation. Clearing its cookie loses access, and recovery is not implemented.
The demo uses free hosting and may take time to wake up.

For development, start with [CONTRIBUTING.md](CONTRIBUTING.md). Storage and hosting
are covered in [PostgreSQL setup](docs/postgres_setup.md) and
[Render deployment](docs/render_deployment.md).

Report suspected vulnerabilities using [the security policy](SECURITY.md).

## License

The project code is available under the [MIT License](LICENSE).

ApexMedic is an independent project, not affiliated with EA or Respawn.
The license does not grant rights to their trademarks or third-party material
linked from the guidance catalogue.
