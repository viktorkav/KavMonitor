# Reddit Feed Digest

Python project that collects Reddit threads and RSS articles, ranks the most relevant items, optionally uses Gemini to generate editor picks and translated headlines, and renders a static HTML briefing.

## What is included

- Configurable subreddit and RSS source lists
- Static HTML report generation
- Optional Gemini-powered summaries and headline translation
- Local admin panel for configuration and manual runs
- Optional remote deployment helper for a Linux host

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp deploy_config.env.example deploy_config.env
```

Set the required values in `.env`, then run:

```bash
python monitor.py
```

To start the local admin panel:

```bash
python admin_app.py
```

## Public repository notes

- Secrets are not stored in this repository.
- Local environment files, deployment overrides, generated output, and archives are ignored by Git.
- The public version is intentionally neutral and does not include personal branding or personal infrastructure details.

## License

This project is distributed under `PolyForm Noncommercial 1.0.0`. See [`LICENSE`](./LICENSE).
