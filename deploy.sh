#!/bin/bash
# Generic deploy helper for the public repository.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_CONFIG_FILE="${SCRIPT_DIR}/deploy_config.env"

if [ -f "$DEPLOY_CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$DEPLOY_CONFIG_FILE"
fi

REMOTE_USER="${REMOTE_USER:-}"
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_PASS="${REMOTE_PASS:-}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-}"

SETUP_CRON="${SETUP_CRON:-1}"
CRON_SCHEDULE="${CRON_SCHEDULE:-0 8 * * *}"
CRON_LOG_FILE="${CRON_LOG_FILE:-logs/monitor.log}"

SETUP_ADMIN_SERVICE="${SETUP_ADMIN_SERVICE:-1}"
ADMIN_HOST="${ADMIN_HOST:-0.0.0.0}"
ADMIN_PORT="${ADMIN_PORT:-5959}"
ADMIN_LOG_FILE="${ADMIN_LOG_FILE:-logs/monitor_admin.log}"
LOCAL_DEPLOY="${LOCAL_DEPLOY:-auto}"

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"

is_local_target() {
    case "$REMOTE_HOST" in
        ""|localhost|127.0.0.1|::1)
            return 0
            ;;
    esac

    if [ "$REMOTE_HOST" = "$(hostname 2>/dev/null)" ]; then
        return 0
    fi

    return 1
}

resolve_deploy_mode() {
    case "$LOCAL_DEPLOY" in
        1|true|yes|on)
            echo "1"
            return 0
            ;;
        0|false|no|off)
            echo "0"
            return 0
            ;;
    esac

    if is_local_target; then
        echo "1"
        return 0
    fi

    if ! command -v sshpass >/dev/null 2>&1; then
        echo "1"
        return 0
    fi

    echo "0"
}

USE_LOCAL_DEPLOY="$(resolve_deploy_mode)"

validate_config() {
    if [ -z "$REMOTE_DIR" ]; then
        echo "REMOTE_DIR is required." >&2
        exit 1
    fi

    if [ "$USE_LOCAL_DEPLOY" = "0" ]; then
        if [ -z "$REMOTE_USER" ] || [ -z "$REMOTE_HOST" ]; then
            echo "REMOTE_USER and REMOTE_HOST are required for remote deploy." >&2
            exit 1
        fi
        if ! command -v sshpass >/dev/null 2>&1; then
            echo "sshpass is required for remote deploy." >&2
            exit 1
        fi
    fi
}

run_remote() {
    local remote_cmd="$1"

    if [ "$USE_LOCAL_DEPLOY" = "1" ]; then
        TERM=dumb bash -c "$remote_cmd"
        return $?
    fi

    sshpass -p "$REMOTE_PASS" ssh \
        -o LogLevel=ERROR \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o PubkeyAuthentication=no \
        -o PreferredAuthentications=password \
        -p "$REMOTE_PORT" "$SSH_TARGET" "$remote_cmd"
}

run_remote_sudo() {
    local remote_cmd="$1"

    if [ "$USE_LOCAL_DEPLOY" = "1" ]; then
        sudo bash -lc "$remote_cmd"
        return $?
    fi

    run_remote "cat > /tmp/_monitor_sudo_cmd.sh << 'SUDOSCRIPT'
${remote_cmd}
SUDOSCRIPT
chmod +x /tmp/_monitor_sudo_cmd.sh"
    run_remote "echo '${REMOTE_PASS}' | sudo -S bash /tmp/_monitor_sudo_cmd.sh 2>/dev/null; rc=\$?; rm -f /tmp/_monitor_sudo_cmd.sh; exit \$rc"
}

run_rsync_to_remote() {
    local source="$1"
    local dest="$2"
    shift 2

    if [ "$USE_LOCAL_DEPLOY" = "1" ]; then
        mkdir -p "$dest"
        rsync -a "$@" "$source" "$dest"
        return 0
    fi

    sshpass -p "$REMOTE_PASS" rsync -rlvz \
        --no-perms --no-times \
        "$@" \
        -e "ssh -p ${REMOTE_PORT}" \
        "$source" \
        "${SSH_TARGET}:${dest}"
}

detect_remote_python() {
    if [ "$USE_LOCAL_DEPLOY" = "1" ]; then
        if command -v python3 >/dev/null 2>&1; then
            echo "python3"
            return 0
        fi
        if command -v python >/dev/null 2>&1; then
            echo "python"
            return 0
        fi
        return 1
    fi

    run_remote "if command -v python3 >/dev/null 2>&1; then printf 'python3\n'; elif command -v python >/dev/null 2>&1; then printf 'python\n'; fi"
}

resolve_remote_path() {
    local path="$1"
    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s/%s\n' "$REMOTE_DIR" "$path"
    fi
}

setup_remote_venv() {
    run_remote "cd '${REMOTE_DIR}' && \
mkdir -p .pip-cache && \
export PIP_CACHE_DIR='${REMOTE_DIR}/.pip-cache' && \
if [ -d .venv ] && { [ ! -x .venv/bin/python ] || ! .venv/bin/python -V >/dev/null 2>&1; }; then rm -rf .venv; fi && \
if [ ! -x .venv/bin/python ]; then ${REMOTE_PYTHON} -m venv .venv; fi && \
if [ ! -x .venv/bin/python ]; then echo 'Failed to prepare remote venv.' >&2; exit 1; fi && \
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then .venv/bin/python -m ensurepip --upgrade; fi && \
.venv/bin/python -m pip install --upgrade pip --quiet && \
.venv/bin/python -m pip install -r requirements.txt --quiet"
}

install_systemd_unit() {
    local unit_name="$1"
    local unit_body="$2"

    run_remote "cat > '${REMOTE_DIR}/${unit_name}' << 'UNIT'
${unit_body}
UNIT"
    run_remote_sudo "mv '${REMOTE_DIR}/${unit_name}' '/etc/systemd/system/${unit_name}'"
}

validate_config

echo "==> Syncing project files"
run_remote "mkdir -p '${REMOTE_DIR}'"
run_rsync_to_remote \
    "${SCRIPT_DIR}/" \
    "${REMOTE_DIR}/" \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'deploy_config.env' \
    --exclude '.venv/' \
    --exclude '.pip-cache/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    --exclude 'output/' \
    --exclude 'generated_output/' \
    --exclude 'relatorios_reddit/' \
    --exclude '.monitor.lock' \
    --exclude '.last_run'

REMOTE_PYTHON="$(detect_remote_python || true)"
if [ -z "$REMOTE_PYTHON" ]; then
    echo "Python 3 is not available on the target host." >&2
    exit 1
fi

if ! run_remote "${REMOTE_PYTHON} -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'"; then
    echo "Python 3.10+ is required on the target host." >&2
    exit 1
fi

echo "==> Installing Python dependencies"
setup_remote_venv
RUN_PYTHON="${REMOTE_DIR}/.venv/bin/python"

CRON_LOG_FILE_ABS="$(resolve_remote_path "$CRON_LOG_FILE")"
ADMIN_LOG_FILE_ABS="$(resolve_remote_path "$ADMIN_LOG_FILE")"
run_remote "mkdir -p '$(dirname "$CRON_LOG_FILE_ABS")' '$(dirname "$ADMIN_LOG_FILE_ABS")'"

if [ "${SETUP_CRON}" = "1" ]; then
    CRON_MINUTE="$(printf '%s\n' "$CRON_SCHEDULE" | awk '{print $1}')"
    CRON_HOUR="$(printf '%s\n' "$CRON_SCHEDULE" | awk '{print $2}')"
    SYSTEMD_ONCALENDAR="*-*-* ${CRON_HOUR}:${CRON_MINUTE}:00"

    echo "==> Installing monitor timer"
    install_systemd_unit "feed-digest.service" "[Unit]
Description=Feed Digest report generator
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${REMOTE_USER:-$USER}
WorkingDirectory=${REMOTE_DIR}
ExecStart=${RUN_PYTHON} monitor.py
StandardOutput=append:${CRON_LOG_FILE_ABS}
StandardError=append:${CRON_LOG_FILE_ABS}
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target"

    install_systemd_unit "feed-digest.timer" "[Unit]
Description=Run Feed Digest on schedule

[Timer]
OnCalendar=${SYSTEMD_ONCALENDAR}
Persistent=true

[Install]
WantedBy=timers.target"

    run_remote_sudo "systemctl daemon-reload && systemctl enable --now feed-digest.timer"
fi

if [ "${SETUP_ADMIN_SERVICE}" = "1" ]; then
    echo "==> Installing admin service"
    install_systemd_unit "feed-digest-admin.service" "[Unit]
Description=Feed Digest admin UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER:-$USER}
WorkingDirectory=${REMOTE_DIR}
Environment=ADMIN_HOST=${ADMIN_HOST}
Environment=ADMIN_PORT=${ADMIN_PORT}
ExecStart=${RUN_PYTHON} admin_app.py
Restart=always
RestartSec=5
StandardOutput=append:${ADMIN_LOG_FILE_ABS}
StandardError=append:${ADMIN_LOG_FILE_ABS}

[Install]
WantedBy=multi-user.target"

    run_remote_sudo "systemctl daemon-reload && systemctl enable --now feed-digest-admin.service"
fi

echo "==> Deploy complete"
echo "Target directory: ${REMOTE_DIR}"
echo "Python runtime: ${RUN_PYTHON}"
