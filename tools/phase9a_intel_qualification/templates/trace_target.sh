#!/bin/bash
set -euo pipefail
if [[ $# -lt 4 ]]; then echo 'usage: trace_target.sh LABEL EXPECT OUTPUT_DIR -- COMMAND...' >&2; exit 64; fi
label=$1; expect=$2; out=$3; shift 3
[[ $1 == -- ]] || { echo 'missing --' >&2; exit 64; }; shift
mkdir -p "$out" "$PHASE9A_WORK/home-$label" "$PHASE9A_WORK/tmp-$label"
trace="$out/$label.trace"; stdout="$out/$label.target.stdout"; xml="$out/$label.syscalls.xml"; summary="$out/$label.network.json"
[[ ! -e $trace && ! -e $xml ]] || { echo "refusing existing trace output: $label" >&2; exit 65; }
xcrun xctrace record --instrument 'System Call Trace' --time-limit 120s --output "$trace" --target-stdout "$stdout" --launch -- /usr/bin/sandbox-exec -f "$PHASE9A_BUNDLE/deny-network.sb" /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$PHASE9A_WORK/home-$label" TMPDIR="$PHASE9A_WORK/tmp-$label" "$@" >"$out/$label.xctrace.log" 2>&1
xcrun xctrace export --input "$trace" --xpath '/trace-toc/run[@number="1"]/data/table[@schema="syscall"]' --output "$xml" >"$out/$label.export.log" 2>&1
python3 "$PHASE9A_BUNDLE/parse_syscalls.py" --xml "$xml" --expect "$expect" --output "$summary"
