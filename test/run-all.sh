#!/usr/bin/env bash
# Every test in the bundle. Nothing here needs a node, a network, or a key.
set -uo pipefail
cd "$(dirname "$0")/.."

status=0
run() {
    echo; echo "=== $1 ==="
    shift
    "$@" || status=1
}

run "contracts"  python3 test/test_registry.py
run "checker"    python3 test/test_checker.py
if [ -d node_modules/jsdom ]; then
    run "end to end" node test/e2e.js
else
    echo; echo "=== end to end ==="
    echo "skipped: run 'npm install' first"
fi

echo
[ $status -eq 0 ] && echo "all suites passed" || echo "some suites failed"
exit $status
