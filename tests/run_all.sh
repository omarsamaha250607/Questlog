#!/usr/bin/env bash
# Runs every automated Questlog check from the tests/ folder. Exit code is non-zero if any group fails.
# Needs: python3 + playwright (chromium), node/npm (axe-core), and for the two database checks a PostgreSQL 16
# reachable via PGHOST/PGPORT/PGUSER/PGPASSWORD (defaults: local socket /tmp/pgtest, port 54329).
# At the end it prints which groups passed or failed, with every FAIL/Traceback line, and writes the same table to the
# GitHub run page (the "Summary" tab) when running in GitHub Actions.
set -u
cd "$(dirname "$0")"
LOGDIR="$(mktemp -d)"
python3 ../tools/build_test_copy.py || { echo "could not build the test copy"; exit 1; }
python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT; sleep 1
NAMES=(); CODES=(); LOGS=(); N=0
run(){ N=$((N+1)); local name="$1"; shift; echo "== $name"
  "$@" 2>&1 | tee "$LOGDIR/$N.log"; local rc=${PIPESTATUS[0]}
  NAMES+=("$name"); CODES+=("$rc"); LOGS+=("$LOGDIR/$N.log"); }
run "sync, accounts, conflicts, devices, restore, reset, deletion, layout (fake Supabase)" python3 -I e2e_sb.py
run "game rules, dates, storage, XSS, scale"                       python3 -I e2e_game.py
run "size limit and the shipped file (library, security policy)"   python3 -I prune_ship.py
run "accessibility scan (axe-core)"                                python3 -I a11y.py
run "database access rules"                                        python3 -I rls_local.py
run "account deletion function"                                    python3 -I del_local.py
FAIL=0; OUT="### Questlog test summary"$'\n\n'"| Group | Result |"$'\n'"|---|---|"$'\n'
echo; echo "================ SUMMARY ================"
for i in "${!NAMES[@]}"; do
  last="$(grep -E '[0-9]+/[0-9]+ passed|views checked' "${LOGS[$i]}" | tail -1)"
  if [ "${CODES[$i]}" = 0 ]; then st="PASSED  ${last}"; else st="FAILED (exit ${CODES[$i]})  ${last}"; FAIL=1; fi
  printf '%-90s %s\n' "${NAMES[$i]}" "$st"; OUT+="| ${NAMES[$i]} | ${st} |"$'\n'
done
if [ $FAIL != 0 ]; then
  echo; echo "---- lines that explain the failure ----"; OUT+=$'\n'"Lines that explain the failure:"$'\n\n```\n'
  for i in "${!NAMES[@]}"; do [ "${CODES[$i]}" = 0 ] && continue
    echo "[${NAMES[$i]}]"; OUT+="[${NAMES[$i]}]"$'\n'
    L="$(grep -nE '^FAIL|Traceback|Error|error:|views with violations: [1-9]' "${LOGS[$i]}" | head -25)"; [ -z "$L" ] && L="(no FAIL or Traceback line; last lines follow)"$'\n'"$(tail -8 "${LOGS[$i]}")"
    echo "$L"; OUT+="$L"$'\n'; done; OUT+=$'```\n'
fi
[ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$OUT" >> "$GITHUB_STEP_SUMMARY"
echo; [ $FAIL = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $FAIL
