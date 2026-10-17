# Review instructions

## Passes (tag every finding with its pass)
- **Bugs**: logic errors, broken edge cases, regressions
- **Security**: injection, authentication gaps, secrets or PII in code/logs
- **Compliance**: the diff matches `specs/<slug>.md`, `plans/<slug>.md` and our design principles

## Severity
- **Important**: breaks behaviour, leaks data, or breaches a policy/spec requirement
- **Nit**: style, naming, readability

## Limits
- Report at most 5 nits; summarise the rest as a count.
- Skip generated paths and anything CI already enforces.

## Verdict
End with a verdict addressed to the human code owner: approve / approve with changes / request changes. The reviewer never approves the PR itself.
