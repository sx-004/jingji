#!/usr/bin/env bash
# 把本轮去重状态与运行报告提交到独立的 state 分支。
# 不用 actions/cache：缓存被逐出会导致每轮重发。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ORIG_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

if [ ! -f state/state.json ]; then
  echo "未找到 state/state.json，跳过状态写入。"
  exit 0
fi
cp state/state.json "$TMP/state.json"
if [ -f report.json ]; then
  cp report.json "$TMP/last-report.json"
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git fetch origin "$STATE_BRANCH" 2>/dev/null || true
if git ls-remote --exit-code --heads origin "$STATE_BRANCH" >/dev/null 2>&1; then
  git checkout -B "$STATE_BRANCH" "origin/$STATE_BRANCH"
else
  git checkout --orphan "$STATE_BRANCH"
  git rm -rf --cached . >/dev/null 2>&1 || true
fi
mkdir -p state
cp "$TMP/state.json" state/state.json
if [ -f "$TMP/last-report.json" ]; then
  cp "$TMP/last-report.json" state/last-report.json
fi
git add -f state/state.json state/last-report.json
if git diff --cached --quiet; then
  echo "状态无变化，无需提交。"
  git checkout -f "$ORIG_BRANCH" >/dev/null 2>&1 || true
  exit 0
fi
git commit -m "chore: 更新去重状态与运行报告 $STAMP"
git push origin "$STATE_BRANCH"
echo "状态已提交到 $STATE_BRANCH 分支。"

# 回到原来的分支，否则工作区会被 state 分支替掉，后面的心跳步骤找不到脚本
git checkout -f "$ORIG_BRANCH" >/dev/null 2>&1 || true
echo "已切回 $ORIG_BRANCH 分支。"
