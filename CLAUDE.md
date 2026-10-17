# ai-sdlc

## Workflow (AI-native SDLC)
This repo uses the ai-native-sdlc plugin. Before any change work, load the matching skill and follow it:
new idea, feature or bug report → `intent` · requirements/design → `spec` · implementation plan → `plan` ·
implement or fix → `build` · review a diff/PR → `review` · deploy/release/rollback → `ship` · evals → `evals` ·
monitoring/incidents → `maintain` · maturity → `audit` · continue / unsure / "what's next" → `start`.
Where every change stands: `python3 .claude/hooks/sdlc_guard.py status`.

## Commands
- Test: `make test`   (healthy: `N passed`)
- Lint: `make lint`   (healthy: only the echoed command, no findings)

## Conventions
- Python 3.10+, standard library only in `src/` unless a plan approves a dependency.
- Package code lives in `src/ai_sdlc/`; every behaviour change comes with a test in `tests/`.
- Artifact prose (intent, spec, plan) in Bahasa Indonesia; file names and section headings in English.

## Architecture
- `src/ai_sdlc/`: the package.
- `tests/`: pytest tests (locked during test-first builds).
- `intent/`, `specs/`, `plans/`: one file per change, shared kebab-case slug.
- `.claude/`, `.github/workflows/`, `REVIEW.md`: gates; changed only by a human, outside agent sessions.

## Source of truth
- Repo-first: `intent/`, `specs/` and `plans/` are authoritative.

## Verifying your work
Run `make test` and `make lint` before reporting any task done, and paste the real output.
If a test fails, fix the code, not the test.

## Things Claude gets wrong
- (add a line whenever the same mistake happens twice)
