#!/bin/bash
# usage: evals/run.sh [eval.json ...]   — runs each eval in a fresh worktree at its base commit
# Needs `claude` and an API key (CI or your own terminal; inside a sandboxed session the API is unreachable).
set -u; root=$(git rev-parse --show-toplevel); pass=0; total=0
for e in "${@:-$root/evals/*.json}"; do
  e=$(cd "$(dirname "$e")" && pwd)/$(basename "$e"); total=$((total+1)); w=$(mktemp -d)/wt
  git -C "$root" worktree add -q --detach "$w" "$(jq -r .base "$e")"
  cp -r "$root/.claude" "$w/" 2>/dev/null; cp "$root/Makefile" "$w/" 2>/dev/null
  echo "== $(jq -r .id "$e"): baseline (no agent) must FAIL"
  if "$root/evals/check.sh" "$e" "$w" >/dev/null; then echo "   INVALID: passes without the agent"; git -C "$root" worktree remove --force "$w"; continue; fi
  (cd "$w" && claude -p "$(jq -r .prompt "$e")" --allowedTools "Read,Glob,Grep,Edit,Bash(make *)" --output-format text >/dev/null 2>&1)
  if "$root/evals/check.sh" "$e" "$w"; then pass=$((pass+1)); fi
  git -C "$root" worktree remove --force "$w"
done
echo "eval pass rate: $pass/$total"; [ "$pass" -eq "$total" ]
