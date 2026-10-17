#!/bin/bash
# usage: evals/check.sh <eval.json> <workdir>   — exit non-zero if any check fails
# eval json: {"id","source","base","prompt","checks":[shell commands; BASE placeholder; multi-line ok]}
e=$1; w=$2; base=$(jq -r .base "$e"); fail=0
while IFS= read -r b; do c=$(echo "$b" | base64 -d); c=${c//BASE/$base}
  if (cd "$w" && bash -c "$c") >/dev/null 2>&1; then echo "PASS  ${c%%$'\n'*}"; else echo "FAIL  ${c%%$'\n'*}"; fail=1; fi
done < <(jq -r '.checks[] | @base64' "$e")
exit $fail
