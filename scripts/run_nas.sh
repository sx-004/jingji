#!/usr/bin/env bash
# NAS 定时运行入口：加载密钥 → 跑一轮 → 失败时推 Telegram 告警。
# 由 NAS 的 crontab 调用，不依赖 GitHub Actions。
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_NAME="$(basename "$REPO_DIR")"
BASE_DIR="${MONITOR_BASE:-/vol2/monitors}"
LOG_DIR="${MONITOR_LOG_DIR:-$BASE_DIR/logs}"
ENV_FILE="${MONITOR_ENV_FILE:-$BASE_DIR/env/$REPO_NAME.env}"
PYTHON="${MONITOR_PYTHON:-$BASE_DIR/venv/bin/python}"
PROXY="${MONITOR_PROXY:-http://127.0.0.1:7890}"
MAIN_ARGS="${MONITOR_ARGS:---send --report report.json}"
LOG_KEEP_DAYS="${MONITOR_LOG_KEEP_DAYS:-14}"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$REPO_NAME-$(date +%Y%m%d).log"
cd "$REPO_DIR" || { echo "无法进入 $REPO_DIR" >&2; exit 1; }

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
# 共享配置（如 SMTP 告警账号）：各仓通用，只填一处。
if [ -f "$BASE_DIR/env/common.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$BASE_DIR/env/common.env"
  set +a
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "$(date -Is) 警告：缺少环境文件 $ENV_FILE" >> "$LOG_FILE"
fi

if [ -n "$PROXY" ]; then
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" ALL_PROXY="$PROXY"
  export https_proxy="$PROXY" http_proxy="$PROXY" all_proxy="$PROXY"
  export NO_PROXY="localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
  export no_proxy="$NO_PROXY"
  export CINDY_FORCE_PROXY=1
fi

# 邮件备用通道：Telegram 挂掉（代理整体不可用）时仍能告警，不依赖 VPN。
notify_email() {
  local text="$1"
  local helper="${MONITOR_EMAIL_HELPER:-$BASE_DIR/bin/notify_email.py}"
  [ -f "$helper" ] || return 0
  if [ -z "${SMTP_USER:-}" ] || [ -z "${SMTP_PASS:-}" ]; then
    return 0
  fi
  "$PYTHON" "$helper" "$text" >> "$LOG_FILE" 2>&1 || true
}

notify_failure() {
  local text="$1"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    return 0
  fi
  local api="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
  local args=(-s -m 25 -X POST "$api"
              --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}"
              --data-urlencode "text=${text}")
  if [ -n "$PROXY" ]; then
    curl "${args[@]}" -x "$PROXY" >/dev/null 2>&1 || true
  else
    curl "${args[@]}" >/dev/null 2>&1 || true
  fi
}

started=$(date +%s)
# shellcheck disable=SC2086
"$PYTHON" main.py $MAIN_ARGS >> "$LOG_FILE" 2>&1
code=$?
elapsed=$(( $(date +%s) - started ))

echo "$(date -Is) $REPO_NAME exit=$code elapsed=${elapsed}s" >> "$LOG_FILE"

# 退出码语义与仓库一致：0=全成功，1=部分源失败但已隔离（正常），2 及以上才需要处理。
if [ "$code" -gt 1 ]; then
  tail_text="$(tail -n 8 "$LOG_FILE" | tr "\n" " " | cut -c1-350)"
  notify_failure "【监控异常】${REPO_NAME} 退出码 ${code}，耗时 ${elapsed} 秒。日志：${tail_text}"
  notify_email "【监控异常】${REPO_NAME} 退出码 ${code}，耗时 ${elapsed} 秒。日志：${tail_text}"
fi

find "$LOG_DIR" -maxdepth 1 -name "$REPO_NAME-*.log" -mtime +"$LOG_KEEP_DAYS" -delete 2>/dev/null || true

exit "$code"
