#!/usr/bin/env bash
# 每日活动心跳：公开仓连续 60 天没有任何活动，GitHub 会停掉定时任务。
# 每天往主分支写一次心跳，保证仓库一直有提交活动（不推 Telegram）。
set -uo pipefail

DAY="$(date -u +%Y-%m-%d)"
FILE="activity/heartbeat.md"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git fetch origin main
git checkout -B main "origin/main"

if [ -f "$FILE" ] && grep -q "^- $DAY" "$FILE"; then
  echo "今天已有心跳：$DAY"
  exit 0
fi

mkdir -p activity
{
  echo "# 仓库活动心跳"
  echo ""
  echo "- $DAY $(date -u +%H:%M) UTC 定时运行正常"
} > "$FILE"
git add "$FILE"
git commit -m "chore: 每日活动心跳 $DAY"
if git push origin main; then
  echo "心跳已提交到 main。"
else
  echo "心跳提交失败（不影响采集结果）：$?" >&2
  exit 0
fi
