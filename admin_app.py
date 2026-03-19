#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monitor admin backend (Flask)."""

import datetime as dt
import os
import re
import shlex
import subprocess
import sys
import threading
from copy import deepcopy
from functools import wraps
from pathlib import Path

import yaml
from dotenv import dotenv_values, load_dotenv
from flask import Flask, Response, flash, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"
DEPLOY_CONFIG_PATH = BASE_DIR / "deploy_config.env"
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"

if VENV_PYTHON.exists():
    RUNTIME_PYTHON = str(VENV_PYTHON)
elif Path(sys.executable).exists():
    RUNTIME_PYTHON = sys.executable
else:
    RUNTIME_PYTHON = "python3"

load_dotenv(ENV_PATH)

DEFAULT_CONFIG = {
    "subreddits": {
        "gaming": [],
        "tech": [],
        "giants": [],
    },
    "rss_feeds": [],
    "ai": {
        "provider": "gemini",
        "gemini": {"model": "gemini-3-flash-preview"},
        "editors_picks": 4,
    },
    "output": {"directory": "generated_output"},
}

DEFAULT_DEPLOY = {
    "REMOTE_USER": "",
    "REMOTE_HOST": "",
    "REMOTE_PASS": "",
    "REMOTE_PORT": "22",
    "REMOTE_DIR": "",
    "SETUP_CRON": "1",
    "CRON_SCHEDULE": "0 8 * * *",
    "CRON_LOG_FILE": "logs/monitor.log",
    "SETUP_ADMIN_SERVICE": "1",
    "ADMIN_HOST": "0.0.0.0",
    "ADMIN_PORT": "5959",
    "ADMIN_LOG_FILE": "logs/monitor_admin.log",
    "LOCAL_DEPLOY": "auto",
}

MANAGED_ENV_KEYS = [
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "GOOGLE_API_KEY",
    "PUBLISH_COMMAND",
]

DEPLOY_KEYS = list(DEFAULT_DEPLOY.keys())
ENV_DEFAULTS = {
    "REDDIT_USER_AGENT": "reddit-feed-digest/1.0",
}

JOB_MAX_LINES = 500
JOB_LOCK = threading.Lock()
JOBS = {
    "monitor": {
        "status": "idle",
        "started_at": "",
        "finished_at": "",
        "return_code": None,
        "command": f"{RUNTIME_PYTHON} monitor.py",
        "log": [],
    },
    "deploy": {
        "status": "idle",
        "started_at": "",
        "finished_at": "",
        "return_code": None,
        "command": "bash deploy.sh",
        "log": [],
    },
}

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("ADMIN_SECRET_KEY", "monitor-admin-local")


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_clean_str(value):
    if value is None:
        return ""
    return str(value).strip()


def _load_yaml(path):
    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml inválido (esperado objeto YAML).")
    return data


def _save_yaml(path, data):
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)


def _load_env(path):
    if not path.exists():
        return {}
    parsed = dotenv_values(path)
    return {k: ("" if v is None else str(v)) for k, v in parsed.items()}


def _format_plain_env_value(value):
    value = str(value or "")
    if re.search(r"\s|#", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _format_shell_env_value(value):
    value = str(value or "")
    if value == "":
        return "''"
    return shlex.quote(value)


def _save_env(path, updates, managed_keys, mode="plain"):
    existing = _load_env(path)
    merged = {**existing, **updates}
    formatter = _format_plain_env_value if mode == "plain" else _format_shell_env_value

    lines = []
    for key in managed_keys:
        if key in merged:
            lines.append(f"{key}={formatter(merged[key])}")

    for key in sorted(k for k in merged if k not in managed_keys):
        lines.append(f"{key}={formatter(merged[key])}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _split_lines(raw_value):
    return [line.strip() for line in raw_value.splitlines() if line.strip()]


def _normalize_subreddit_name(raw_value):
    value = _to_clean_str(raw_value)
    value = re.sub(r"^https?://(?:www\.)?reddit\.com/r/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^/?r/", "", value, flags=re.IGNORECASE)
    value = value.strip("/")
    return value


def _parse_subreddit_lines(raw_value, label, required=False):
    entries = []
    seen = set()

    for index, line in enumerate(_split_lines(raw_value), start=1):
        name = _normalize_subreddit_name(line)
        if not name:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", name):
            raise ValueError(
                f"{label}: valor inválido na linha {index} ({line!r}). Use apenas letras, números e _."
            )

        dedupe_key = name.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entries.append(name)

    if required and not entries:
        raise ValueError(f"Lista de subreddits de {label.lower()} não pode ficar vazia.")

    return entries


def _parse_rss_lines(raw_value):
    feeds = []
    for index, line in enumerate(_split_lines(raw_value), start=1):
        if "|" not in line:
            raise ValueError(f"RSS linha {index} inválida. Use o formato Nome|URL.")
        name, url = [part.strip() for part in line.split("|", 1)]
        if not name or not url:
            raise ValueError(f"RSS linha {index} inválida. Nome e URL são obrigatórios.")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"RSS linha {index} inválida. URL deve começar com http:// ou https://.")
        feeds.append({"name": name, "url": url})
    return feeds


def _build_form_data():
    cfg = _load_yaml(CONFIG_PATH)
    env_values = _load_env(ENV_PATH)
    deploy_values = {**DEFAULT_DEPLOY, **_load_env(DEPLOY_CONFIG_PATH)}

    sub_cfg = cfg.get("subreddits", {}) if isinstance(cfg.get("subreddits", {}), dict) else {}
    ai_cfg = cfg.get("ai", {}) if isinstance(cfg.get("ai", {}), dict) else {}
    gem_cfg = ai_cfg.get("gemini", {}) if isinstance(ai_cfg.get("gemini", {}), dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg.get("output", {}), dict) else {}

    return {
        "gaming_subs": "\n".join(sub_cfg.get("gaming", []) or []),
        "tech_subs": "\n".join(sub_cfg.get("tech", []) or []),
        "giants_subs": "\n".join(sub_cfg.get("giants", []) or []),
        "rss_feeds": "\n".join(
            f"{item.get('name', '').strip()}|{item.get('url', '').strip()}"
            for item in (cfg.get("rss_feeds", []) or [])
            if isinstance(item, dict) and item.get("name") and item.get("url")
        ),
        "ai_provider": _to_clean_str(ai_cfg.get("provider") or "gemini"),
        "ai_model": _to_clean_str(gem_cfg.get("model") or "gemini-3-flash-preview"),
        "editors_picks": _to_clean_str(ai_cfg.get("editors_picks") or 4),
        "output_directory": _to_clean_str(output_cfg.get("directory") or "generated_output"),
        **{key: _to_clean_str(env_values.get(key, ENV_DEFAULTS.get(key, ""))) for key in MANAGED_ENV_KEYS},
        **{key: _to_clean_str(deploy_values.get(key, "")) for key in DEPLOY_KEYS},
    }


def _apply_settings(form_data):
    cfg = _load_yaml(CONFIG_PATH)
    cfg.setdefault("subreddits", {})
    cfg.setdefault("ai", {})
    cfg["ai"].setdefault("gemini", {})
    cfg.setdefault("output", {})

    gaming = _parse_subreddit_lines(form_data.get("gaming_subs", ""), "Games", required=True)
    tech = _parse_subreddit_lines(form_data.get("tech_subs", ""), "Tech", required=True)
    giants = _parse_subreddit_lines(form_data.get("giants_subs", ""), "Giants", required=False)

    editors_picks_raw = _to_clean_str(form_data.get("editors_picks", "4"))
    try:
        editors_picks = int(editors_picks_raw)
    except ValueError as exc:
        raise ValueError("editors_picks precisa ser número inteiro.") from exc
    if editors_picks < 1 or editors_picks > 12:
        raise ValueError("editors_picks deve ficar entre 1 e 12.")

    rss_feeds = _parse_rss_lines(form_data.get("rss_feeds", ""))

    cfg["subreddits"]["gaming"] = gaming
    cfg["subreddits"]["tech"] = tech
    cfg["subreddits"]["giants"] = giants
    cfg["rss_feeds"] = rss_feeds
    cfg["ai"]["provider"] = _to_clean_str(form_data.get("ai_provider", "gemini")) or "gemini"
    cfg["ai"]["gemini"]["model"] = (
        _to_clean_str(form_data.get("ai_model", "gemini-3-flash-preview")) or "gemini-3-flash-preview"
    )
    cfg["ai"]["editors_picks"] = editors_picks
    cfg["output"]["directory"] = (
        _to_clean_str(form_data.get("output_directory", "generated_output")) or "generated_output"
    )

    deploy_updates = {
        key: _to_clean_str(form_data.get(key, DEFAULT_DEPLOY.get(key, "")))
        for key in DEPLOY_KEYS
    }
    deploy_updates["SETUP_CRON"] = "1" if form_data.get("SETUP_CRON") == "1" else "0"
    deploy_updates["SETUP_ADMIN_SERVICE"] = "1" if form_data.get("SETUP_ADMIN_SERVICE") == "1" else "0"
    remote_requested = any(
        deploy_updates.get(key)
        for key in ("REMOTE_HOST", "REMOTE_USER", "REMOTE_PASS", "REMOTE_DIR")
    ) or deploy_updates.get("LOCAL_DEPLOY") == "0"

    if remote_requested and not deploy_updates["REMOTE_HOST"]:
        raise ValueError("REMOTE_HOST is required for remote deploy.")
    if remote_requested and not deploy_updates["REMOTE_USER"]:
        raise ValueError("REMOTE_USER is required for remote deploy.")
    if remote_requested and not deploy_updates["REMOTE_DIR"]:
        raise ValueError("REMOTE_DIR is required for remote deploy.")
    if not deploy_updates["REMOTE_PORT"]:
        deploy_updates["REMOTE_PORT"] = "22"
    if not deploy_updates["CRON_SCHEDULE"]:
        deploy_updates["CRON_SCHEDULE"] = DEFAULT_DEPLOY["CRON_SCHEDULE"]
    if not deploy_updates["CRON_LOG_FILE"]:
        deploy_updates["CRON_LOG_FILE"] = DEFAULT_DEPLOY["CRON_LOG_FILE"]
    if not deploy_updates["ADMIN_HOST"]:
        deploy_updates["ADMIN_HOST"] = DEFAULT_DEPLOY["ADMIN_HOST"]
    if not deploy_updates["ADMIN_PORT"]:
        deploy_updates["ADMIN_PORT"] = DEFAULT_DEPLOY["ADMIN_PORT"]
    if not deploy_updates["ADMIN_LOG_FILE"]:
        deploy_updates["ADMIN_LOG_FILE"] = DEFAULT_DEPLOY["ADMIN_LOG_FILE"]
    for port_key in ("REMOTE_PORT", "ADMIN_PORT"):
        try:
            if int(deploy_updates[port_key]) < 1:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"{port_key} precisa ser inteiro positivo.") from exc

    env_updates = {
        key: _to_clean_str(form_data.get(key, ""))
        for key in MANAGED_ENV_KEYS
    }

    _save_yaml(CONFIG_PATH, cfg)
    _save_env(ENV_PATH, env_updates, MANAGED_ENV_KEYS, mode="plain")
    _save_env(DEPLOY_CONFIG_PATH, deploy_updates, DEPLOY_KEYS, mode="shell")


def _is_auth_enabled():
    return bool(os.getenv("ADMIN_USERNAME") and os.getenv("ADMIN_PASSWORD"))


def _is_valid_auth(username, password):
    return (
        username == os.getenv("ADMIN_USERNAME")
        and password == os.getenv("ADMIN_PASSWORD")
    )


def requires_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_auth_enabled():
            return view(*args, **kwargs)

        auth = request.authorization
        if auth and _is_valid_auth(auth.username, auth.password):
            return view(*args, **kwargs)

        return Response(
            "Autenticação necessária.",
            401,
            {"WWW-Authenticate": 'Basic realm="Monitor Admin"'},
        )

    return wrapper


def _append_job_log(job_name, line):
    with JOB_LOCK:
        target = JOBS[job_name]["log"]
        target.append(f"[{now_str()}] {line}")
        if len(target) > JOB_MAX_LINES:
            del target[:-JOB_MAX_LINES]


def _run_job(job_name, command):
    try:
        _append_job_log(job_name, f"Executando: {' '.join(command)}")
        proc = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        if proc.stdout:
            for raw_line in proc.stdout:
                _append_job_log(job_name, raw_line.rstrip())

        return_code = proc.wait()
        with JOB_LOCK:
            JOBS[job_name]["status"] = "success" if return_code == 0 else "failed"
            JOBS[job_name]["return_code"] = return_code
            JOBS[job_name]["finished_at"] = now_str()
        _append_job_log(job_name, f"Finalizado com código {return_code}.")
    except Exception as exc:
        with JOB_LOCK:
            JOBS[job_name]["status"] = "failed"
            JOBS[job_name]["return_code"] = -1
            JOBS[job_name]["finished_at"] = now_str()
        _append_job_log(job_name, f"Falha ao executar job: {exc}")


def _start_job(job_name, command):
    with JOB_LOCK:
        if JOBS[job_name]["status"] == "running":
            return False
        JOBS[job_name]["status"] = "running"
        JOBS[job_name]["started_at"] = now_str()
        JOBS[job_name]["finished_at"] = ""
        JOBS[job_name]["return_code"] = None
        JOBS[job_name]["log"] = []

    worker = threading.Thread(target=_run_job, args=(job_name, command), daemon=True)
    worker.start()
    return True


def _job_snapshot():
    with JOB_LOCK:
        return deepcopy(JOBS)


@app.get("/")
@requires_auth
def index():
    jobs = _job_snapshot()
    any_running = any(job.get("status") == "running" for job in jobs.values())
    return render_template(
        "admin.html",
        data=_build_form_data(),
        jobs=jobs,
        any_running=any_running,
        auth_enabled=_is_auth_enabled(),
    )


@app.post("/save")
@requires_auth
def save_settings():
    try:
        _apply_settings(request.form)
        flash("Configurações salvas com sucesso.", "success")
    except Exception as exc:
        flash(f"Erro ao salvar: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/run-monitor")
@requires_auth
def run_monitor():
    started = _start_job("monitor", [RUNTIME_PYTHON, "monitor.py"])
    if started:
        flash("Execução do monitor iniciada.", "success")
    else:
        flash("O monitor já está em execução.", "warning")
    return redirect(url_for("index"))


@app.post("/run-deploy")
@requires_auth
def run_deploy():
    started = _start_job("deploy", ["bash", "deploy.sh"])
    if started:
        flash("Deploy iniciado.", "success")
    else:
        flash("Já existe um deploy em execução.", "warning")
    return redirect(url_for("index"))


@app.get("/health")
def health():
    """Health check: returns last monitor run status from .last_run file."""
    health_file = BASE_DIR / ".last_run"
    if not health_file.exists():
        return {"status": "no_data", "message": "Monitor has not run yet"}, 503

    import json as _json
    try:
        data = _json.loads(health_file.read_text())
        last_run = dt.datetime.fromisoformat(data["timestamp"])
        age_hours = (dt.datetime.now() - last_run).total_seconds() / 3600
        data["age_hours"] = round(age_hours, 1)
        data["stale"] = age_hours > 26  # Should run daily; 26h gives some buffer
        status_code = 200 if not data["stale"] else 503
        return data, status_code
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


def _ensure_auth_configured():
    """Generate random admin credentials on first start if none are set."""
    if _is_auth_enabled():
        return

    import secrets
    password = secrets.token_urlsafe(16)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = password
    print(f"\n{'='*50}")
    print("  Monitor Admin - auto-generated credentials:")
    print(f"  Username: admin")
    print(f"  Password: {password}")
    print(f"  Set ADMIN_USERNAME and ADMIN_PASSWORD in .env to use your own.")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    _ensure_auth_configured()
    host = os.getenv("ADMIN_HOST", "0.0.0.0")
    port = int(os.getenv("ADMIN_PORT", "5959"))
    debug = os.getenv("ADMIN_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
