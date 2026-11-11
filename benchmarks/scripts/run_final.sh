#!/bin/bash
# Held-out MemDerive answers, staleness audits and the MEME second reader, in one sequential run.
# See ../PREREGISTRATION.md. Reuses existing extraction and simulations; makes no extract or
# simulate calls. Stops at the first step that fails twice. Run from anywhere:
#   caffeinate -i bash benchmarks/scripts/run_final.sh > benchmarks/out/final_run.log 2>&1
set -uo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.."
export PYTHONPATH=../src

if grep -v '^#' "$SELF" | grep -qE -- '-f[a]st'; then  # bracket keeps this line from matching
  echo "=== STOPPED: a fast model is named in $SELF"; exit 1
fi
echo "=== sha256 of the pre-registration and benchmark code"
shasum -a 256 PREREGISTRATION.md memderive.py meme.py stale_audit.py longmemeval.py lme_models.py \
  scripts/run_final.sh scripts/md_gate.py

M="uv run --extra bench python memderive.py"
ME="uv run --extra bench python meme.py"
AUDIT="uv run --extra bench python stale_audit.py"
GATE="python3 scripts/md_gate.py"
ALL="tripops_w2 shopops_w2 eduops_w2 tripops_w3 tripops_w4 shopops_w3 eduops_w3"
C=cursor:composer-2.5
G=cursor:grok-4.7-low
MC="--max-calls 16"
OUT=$(mktemp -d)

attempt() {  # run once; fail on a non-zero exit or a reported error
  "$@" 2>&1 | tee "$OUT/last"
  local rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] && ! grep -qE '^[0-9]+ errors, e\.g\.' "$OUT/last"
}

step() {  # retry once: the response cache turns a retry into a resume
  echo "=== $(date '+%F %T') $*"
  attempt "$@" && return
  echo "=== $(date '+%F %T') retrying: $*"
  attempt "$@" && return
  echo "=== STOPPED: step failed twice: $*"
  exit 1
}

# 0. Every held-out world has a complete extraction and simulation (answer skips missing ones).
step $GATE extract $ALL
step $GATE simulate $ALL

# 1. Smoke test of the answerer on the dev world.
step $M answer --worlds tripops_w1 --systems eager full_context --answerers $C $MC
step $GATE parsed cursor_composer-2.5 tripops_w1 eager full_context
step $M report --worlds tripops_w1 --systems eager full_context full_rebuild --answerers $C \
  --tag dev_smoke

# 2. Held-out answers. eager first: facts and the priority systems often share its prompts.
step $M answer --worlds $ALL --systems eager --answerers $C $MC
step $M answer --worlds $ALL --systems facts priority_15 stale_priority_15 --answerers $C $MC
step $M answer --worlds $ALL --systems untracked bm25 --answerers $C $MC
step $M answer --worlds $ALL --systems full_context --answerers $C $MC
for w in $ALL; do  # unparsed answers would silently score as wrong
  step $GATE parsed cursor_composer-2.5 $w eager facts priority_15 stale_priority_15 untracked \
    bm25 full_context
done
step $M report --worlds $ALL --answerers $C --tag heldout --systems eager facts priority_15 \
  stale_priority_15 untracked bm25 full_context full_rebuild periodic

# 3. Staleness audits of the pages that stale-serving systems show.
step $AUDIT --system untracked --limit 250 --judge $G $MC
step $AUDIT --system stale_priority_15 --limit 142 --judge $G $MC

# 4. MEME second reader: grok answers; composer (primary) and grok judge.
step $ME answer --episodes 50 --systems full_context strata --answerers $G $MC
step $ME judge --episodes 50 --systems full_context strata --answerers $G --judges $C $MC
step $ME judge --episodes 50 --systems full_context strata --answerers $G --judges $G $MC
step $ME report --episodes 50 --answerers $G --judges $C $G --tag grok50

# 5. Descriptive: a third budget point.
step $M answer --worlds $ALL --systems priority_40 --answerers $C $MC
for w in $ALL; do step $GATE parsed cursor_composer-2.5 $w priority_40; done
step $M report --worlds $ALL --answerers $C --tag heldout_p40 --systems facts priority_15 \
  priority_40 eager periodic untracked full_rebuild

echo "=== FINAL_DONE $(date '+%F %T')"
