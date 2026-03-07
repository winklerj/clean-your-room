# Getting Started with Clean Your Room

By the end of this tutorial you will have:

- A running Clean Your Room instance on your machine
- A GitHub repository cloned and tracked
- An AI-generated formal specification committed to your local specs monorepo

## Prerequisites

You need:

- **Python 3.12**
- **uv** package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **An Anthropic API key** exported as `ANTHROPIC_API_KEY`

## 1. Clone and install

```bash
git clone https://github.com/your-org/clean-your-room.git
cd clean-your-room
uv sync --extra dev
```

## 2. Start the app

```bash
uv run uvicorn clean_room.main:app --reload
```

You should see output ending with:

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

## 3. Open the dashboard

Go to [http://localhost:8000](http://localhost:8000) in your browser.

You will see the Clean Your Room dashboard with an empty repo list and a link to **Add Repo**.

## 4. Add a GitHub repository

1. Click **Add Repo**.
2. Paste a GitHub URL into the input field, for example: `https://github.com/pallets/flask`
3. Click **Clone & Add**.

Clean Your Room clones the repo to `~/.clean-room/repos/` and redirects you to the repo detail page.

## 5. Create a job

On the repo detail page:

1. Select a prompt from the dropdown. Clean Your Room ships with two defaults:
   - **Create Spec** -- generates a new formal specification for the repo
   - **Improve Spec** -- refines an existing specification
2. Choose **Create Spec** for your first run.
3. Optionally add a feature description to narrow the agent's focus.
4. Click **Create Job**.

You are redirected to the job viewer.

## 6. Watch the job run

The job viewer streams logs in real time via SSE (Server-Sent Events). You will see output like:

```
=== Starting iteration 1/20 ===
...agent output...
=== Completed iteration 1/20 ===
=== Starting iteration 2/20 ===
...
```

The agent iterates up to 20 times by default, reading the repo and building the specification. You can click **Stop** at any time to halt early.

## 7. Find the generated spec

When the job completes, Clean Your Room copies the generated spec files into the specs monorepo:

```bash
ls ~/.clean-room/specs-monorepo/
```

Inside the folder named after your repo's slug (e.g., `pallets-flask/`), you will find the specification files the agent produced.

The specs monorepo is a local git repository. Each completed job creates a commit:

```bash
cd ~/.clean-room/specs-monorepo
git log --oneline
```

## Next steps

- Run **Improve Spec** on the same repo to refine the generated specification.
- Add more repositories from the dashboard.
- Set the `DEFAULT_MODEL` environment variable to use a different Claude model (default: `claude-sonnet-4-20250514`).
- Set `CLEAN_ROOM_DIR` to change the data directory (default: `~/.clean-room`).
