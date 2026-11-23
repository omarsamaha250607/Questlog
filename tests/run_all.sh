#!/usr/bin/env bash
# Runs every automated Questlog check from the tests/ folder. Exit code is non-zero if any check fails.
# Needs: python3 + playwright (chromium), node/npm (axe-core), and for the two database checks a PostgreSQL 16
# reachable via PGHOST/PGPORT/PGUSER/PGPASSWORD (defaults: local socket /tmp/pgtest, port 54329).
set -u
cd "$(dirname "$0")"
python3 ../tools/build_test_copy.py
python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT; sleep 1
FAIL=0
run(){ echo "== $1"; shift; "$@" || FAIL=1; }
run "sync, accounts, conflicts, devices, restore, reset, deletion, layout (fake Supabase)" python3 -I e2e_sb.py
run "game rules, dates, storage, XSS, scale"                       python3 -I e2e_game.py
run "size limit and the shipped file (library, security policy)"   python3 -I prune_ship.py
run "accessibility scan (axe-core)"                                python3 -I a11y.py
run "database access rules"                                        python3 -I rls_local.py
run "account deletion function"                                    python3 -I del_local.py
echo; [ $FAIL = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $FAIL
