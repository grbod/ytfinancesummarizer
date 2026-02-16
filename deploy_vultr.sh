#!/usr/bin/env bash
set -euo pipefail

# Usage:
# VULTR_HOST=your.server.ip VULTR_USER=root VULTR_PASSWORD='...' ./deploy_vultr.sh
#
# Optional overrides:
# REPO_URL=https://github.com/grbod/ytfinancesummarizer.git
# APP_DIR=/opt/ytfinancesummarizer
# BRANCH=main
# CRON_TIME='30 21 * * *'  # 4:30 PM EST

VULTR_HOST="${VULTR_HOST:-}"
VULTR_USER="${VULTR_USER:-root}"
VULTR_PASSWORD="${VULTR_PASSWORD:-}"
REPO_URL="${REPO_URL:-https://github.com/grbod/ytfinancesummarizer.git}"
APP_DIR="${APP_DIR:-/opt/ytfinancesummarizer}"
BRANCH="${BRANCH:-main}"
CRON_TIME="${CRON_TIME:-30 21 * * *}"

if [[ -z "${VULTR_HOST}" || -z "${VULTR_PASSWORD}" ]]; then
  echo "Missing required env vars: VULTR_HOST and VULTR_PASSWORD" >&2
  exit 1
fi

SSH_OPTS=(
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)

sshpass -p "${VULTR_PASSWORD}" ssh "${SSH_OPTS[@]}" "${VULTR_USER}@${VULTR_HOST}" \
  "REPO_URL='${REPO_URL}' APP_DIR='${APP_DIR}' BRANCH='${BRANCH}' CRON_TIME='${CRON_TIME}' bash -s" <<'REMOTE'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip tzdata ca-certificates

mkdir -p "$(dirname "${APP_DIR}")"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  rm -rf "${APP_DIR}"
  git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
else
  cd "${APP_DIR}"
  git fetch origin "${BRANCH}"
  git checkout "${BRANCH}"
  git reset --hard "origin/${BRANCH}"
fi

cd "${APP_DIR}"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created ${APP_DIR}/.env from template. Fill in keys before first scheduled run."
fi

CRON_CMD="cd ${APP_DIR} && ${APP_DIR}/venv/bin/python main.py >> ${APP_DIR}/cron.log 2>&1"
CRON_LINE="${CRON_TIME} ${CRON_CMD}"

{
  crontab -l 2>/dev/null | grep -v "${APP_DIR}/venv/bin/python main.py" | grep -v "^CRON_TZ=" || true
  echo "${CRON_LINE}"
} | crontab -

echo "Deployment completed on $(hostname)."
echo "Cron schedule installed: ${CRON_LINE}"
REMOTE
