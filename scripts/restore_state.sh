#!/usr/bin/env bash
# 从 state 分支恢复去重状态到工作区，避免每轮都从零开始。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
mkdir -p state
if git fetch origin "$STATE_BRANCH" 2>/dev/null; then
  if git cat-file -e "origin/$STATE_BRANCH:state/state.json" 2>/dev/null; then
    git show "origin/$STATE_BRANCH:state/state.json" > state/state.json
    echo "已从 $STATE_BRANCH 分支恢复去重状态。"
  else
    echo "$STATE_BRANCH 分支还没有 state.json，本轮按首次运行处理。"
  fi
else
  echo "$STATE_BRANCH 分支尚不存在，本轮按首次运行处理（稍后由 persist_state.sh 建分支）。"
fi
