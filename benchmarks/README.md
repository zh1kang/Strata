# Real-data benchmark: LongMemEval-S

`longmemeval.py` runs Strata and baselines on [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
(S setting, ~115k-token haystacks of ~50 sessions per question), using the cleaned release
`xiaowu0162/longmemeval-cleaned`. It is separate from the synthetic generator in `strata.bench`.

## Reproduce

```sh
mkdir -p benchmarks/data
curl -L -o benchmarks/data/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

# needs the `cursor-agent` CLI (logged in) for the default models; no extra Python packages
PYTHONPATH=src python3 benchmarks/longmemeval.py run --n 150 --extra-ku 40 --tag main \
  --answerers cursor:composer-2.5-fast cursor:grok-4.7-low-fast cursor:cursor-grok-4.5-low-fast
```

Model specs are `cursor:<id>` (Cursor agent CLI, read-only `ask` mode in an empty workspace) or
`openai:<model>` (chat completions, needs credit on `OPENAI_API_KEY`). Every response is cached in
`benchmarks/out/longmemeval/responses.sqlite`, and per-question artifacts (fact logs, retrieved
contexts, answers, verdicts) are written under `benchmarks/out/longmemeval/`, so re-running reuses
them and `report` regenerates the tables without model calls. Reports go to
`benchmarks/results/longmemeval_<tag>.{md,json}`.

Question selection is a seeded, type-stratified ranking; `--n N` takes the first N, so a larger run
extends a smaller one. `--extra-ku K` appends K further knowledge-update questions.

## Systems (same events, same context caps, same answer prompt for all)

| system | memory used at query time |
| --- | --- |
| `raw_search` | BM25 (FTS5) over raw round events: `search_events`, top 10 |
| `strata_eager` | LLM-extracted facts, a `profile` node per subject and a `timeline` node per fact whose value changed, all rebuilt without budget after every session; `recall(events=10)` |
| `strata_priority` | same, background budget of 1 cost unit per session, priority policy |
| `strata_lazy` | same facts and nodes, no background maintenance; `recall(events=10, lazy_budget=8)` builds or repairs the nodes a query matched |
| `stale_control` | FIFO background budget 1 per session, but `recall(include_stale=True)` and withheld nodes are shown as if current. Not a real strategy |

Strata systems get up to 10 current items (facts, profiles, timelines) followed by up to 10 raw
round events: the source round of each returned fact, interleaved with plain event search hits.
Every system's context is cut at the same 24,000 characters. A `timeline` lists a fact's current
value with the date it was stated and its earlier values with theirs; it goes stale whenever the
fact changes, so it is the node that exercises withholding and repair. Profiles are indexed by
attribute names only, so they do not match every query one of their values matches.

Facts are extracted once per question by one model (shared by all Strata variants), session by
session in date order, with the existing slot names shown so updates reuse the same slot. A reply
with stray text before or after its JSON object is accepted; every fact is still validated and its
evidence quote must occur in that session's user messages.
See the module docstring and report for caps and limitations.
