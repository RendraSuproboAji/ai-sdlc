# Evals
One JSON per task: `{"id", "source", "base", "prompt", "checks": [...]}`.
- `base`: the commit where the task starts (the bug still present).
- `checks`: shell commands that must all pass after the agent's work. `BASE` is replaced by the base commit. Multi-line is fine.
Every eval must FAIL on `base` with no agent (`run.sh` refuses evals that don't).
Add one for every incident and every escaped review finding.
