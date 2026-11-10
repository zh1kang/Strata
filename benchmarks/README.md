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
PYTHONPATH=src python3 benchmarks/longmemeval.py run --tag v3 --subset 150 40 \
  --subset-only-answerers --answerers cursor:composer-2.5-fast cursor:grok-4.7-low-fast
```

Model specs are `cursor:<id>` (Cursor agent CLI, read-only `ask` mode in an empty workspace) or
`openai:<model>` (chat completions, needs credit on `OPENAI_API_KEY`). Every response is cached in
`benchmarks/out/longmemeval/responses.sqlite`, and per-question artifacts (fact logs, retrieved
contexts, answers, verdicts) are written under `benchmarks/out/longmemeval/`, so re-running reuses
them and `report` regenerates the tables without model calls. Reports go to
`benchmarks/results/longmemeval_<tag>.{md,json}`.

Question selection is a seeded, type-stratified ranking; `--n N` takes the first N (default: all
500), so a larger run extends a smaller one. `--extra-ku K` appends K further knowledge-update
questions. `--subset N K` also writes a report restricted to `--n N --extra-ku K` (the 190
questions of the earlier `main` run are `--subset 150 40`). With `--subset-only-answerers`, answer
models after the first answer only those subset questions, and the full report covers the first
model alone. The digest systems run on the first
`--digest-n` questions (default 40) and are answered by the first `--digest-answerers` models.

## Systems (same events, same context cap, same answer prompt for all)

| system | memory used at query time |
| --- | --- |
| `raw_search` | Plain baseline: BM25 (FTS5) over raw round events, `search_events`, top 10, each cut at 3,000 characters |
| `raw_search_tuned` | Matched baseline: the same event search with every retrieval aid the Strata systems get (snippets, time windows, filled to the same cap), but no facts |
| `strata_no_nodes` | LLM-extracted facts plus `recall(events=20)`, no derived nodes |
| `strata_eager` | Facts, a `topic` node per extracted topic and a `timeline` node per single-valued fact whose value changed, all repaired without budget after every session |
| `strata_priority` | Same, background budget of 1 cost unit per session, priority policy |
| `strata_lazy` | Same facts and nodes, no background maintenance; `recall(lazy_budget=8)` builds or repairs the nodes a query matched |
| `stale_control` | FIFO background budget 1 per session, but withheld nodes are shown as if current. Not a real strategy |
| `digest_eager` / `digest_priority` / `digest_lazy` / `digest_stale` | `strata_*` plus an LLM-written digest per topic (level 2, reads the topic node and its facts' histories), charged its real token usage; budgets of 1,500 tokens per session (priority, stale) or 6,000 tokens per query (lazy) |

Every system except `raw_search` renders each item as a query-centred snippet of at most 1,500
characters (`strata.index.snippet`), and, when the question names a relative time ("last month",
"two weeks ago", "in March"), adds up to 10 events from a generous window around it next to the
normal hits. Every context is cut at 24,000 characters. Strata systems get up to 10 current items
(facts, topics, timelines, digests) followed by up to 20 round events: the source round of each
returned fact, interleaved with event search hits. Their event index also holds each round's
extracted facts, which raw search does not have.

Facts are extracted once per question by one model (shared by all Strata variants), session by
session in date order, with the existing slot and topic names shown so updates reuse them. Each fact
has an `op`: `set` replaces a single-valued attribute, `add` / `remove` maintain a collection (books
read, plants owned) so later additions do not overwrite earlier ones, `retract` ends a fact. A reply
with stray text before or after its JSON object is accepted; every fact is still validated and its
evidence quote must occur in that session's user messages.

Answers use a note-taking prompt (Chain-of-Note style: notes per relevant item, then a final
`ANSWER:` line). Judges and the knowledge-update label see only the final answer. The report charges
each system the tokens it spent building memory (extraction, digests) next to its answer tokens.
See the module docstring and report for caps and limitations.
