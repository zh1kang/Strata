# Benchmarks

This folder holds four benchmarks; the synthetic workload in `strata.bench` is described at the end:

- `longmemeval.py`: LongMemEval-S, retrieval and answering over long chat histories.
- `memderive.py`: MemDerive (DeriveMem worlds), the maintenance benchmark: incremental against full rebuild, budgets, withholding, dependency tracking.
- `meme.py`: MEME, facts that depend on other facts and must change when those change.
- `perf.py`: engine speed on a synthetic workload, with no model.

`PREREGISTRATION.md` fixed the claims and primary metrics of the held-out MemDerive run and the MEME second-reader run before any of those answers existed.
`scripts/run_final.sh` ran them in order, with checks between steps, and printed the SHA-256 of every script first.
After the run, `memderive.py` gained report-only additions (an abstention column, witnessed comparisons, clearer labels) and the reports were regenerated from the saved answers; the pre-registered numbers did not change.
The top-level README summarises the results. The reports in `results/` are:

| File | Contents |
| --- | --- |
| `memderive_heldout.{md,json}` | MemDerive held-out run: all systems, paired differences, maintenance cost, incremental against full rebuild |
| `memderive_heldout_p40.{md,json}` | The same run with the `priority_40` budget point added |
| `memderive_dev_check.{md,json}` | Development world `tripops_w1`, the source of the development numbers in `PREREGISTRATION.md` |
| `stale_audit_*_heldout.json` | Staleness audits of `untracked` and `stale_priority_15` |
| `stale_audit_blind_check.json` | Blind check of 40 audit verdicts |
| `memderive_budget.png` | Accuracy against maintenance tokens (`scripts/plot_budget.py`) |
| `meme_grok50.{md,json}` | MEME pre-registered second reader: 50 episodes, `grok-4.7-low` answers |
| `meme_full.{md,json}` | MEME, 100 episodes, `composer-2.5-fast` answers |
| `longmemeval_v3.{md,json}` | LongMemEval-S, 500 questions, `composer-2.5-fast` answers |
| `longmemeval_v3_subset190.{md,json}` | LongMemEval-S, 190-question subset, with `grok-4.7-low-fast` as a second answer model |
| `perf.json` | Engine speed |
| `final_run.log` | Log of `scripts/run_final.sh`, with the SHA-256 of `PREREGISTRATION.md` and of each script, without progress lines |

## LongMemEval-S

`longmemeval.py` runs Strata and baselines on [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
(S setting, ~115k-token haystacks of ~50 sessions per question), using the cleaned release
`xiaowu0162/longmemeval-cleaned`. It is separate from the synthetic generator in `strata.bench`.

### Reproduce

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
questions. `--subset N K` also writes a report restricted to `--n N --extra-ku K` (`--subset 150 40`
gives the 190-question subset in `longmemeval_v3_subset190`). With `--subset-only-answerers`, answer
models after the first answer only those subset questions, and the full report covers the first
model alone. The digest systems run on the first
`--digest-n` questions (default 40) and are answered by the first `--digest-answerers` models.

### Systems (same events, same context cap, same answer prompt for all)

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

## MemDerive

`memderive.py` uses the DeriveMem worlds (code Apache-2.0, data CC BY 4.0), checked out under `data/derivemem` or at `$DERIVEMEM`.
Each world is a log of about 450 dated events in about 77 sessions.
Its questions have answers derived from facts and versioned rules, and the dataset's oracle recompiles the gold at 5 checkpoints (20% to 100% of sessions), so answers change as the log grows and scoring needs no judge.
Development used `tripops_w1`; the held-out run used `tripops_w2`, `tripops_w3`, `tripops_w4`, `shopops_w2`, `shopops_w3`, `eduops_w2` and `eduops_w3` (4,267 question-checkpoint cells).

```sh
# from benchmarks/, after the extract and simulate steps (see the module docstring)
bash scripts/run_final.sh          # answers, reports, staleness audits, MEME second reader
uv run --extra bench --with matplotlib python scripts/plot_budget.py
```

One LLM fact extraction per world (`composer-2.5-fast`) feeds every Strata system.
Systems with pages keep one LLM-written page per subject; a page reads its subject's facts, the subjects those facts name and the policy facts through tracked reads, and is charged its locally counted tokens.
The systems differ only in when pages are rebuilt and whether out-of-date pages are served:

| system | pages |
| --- | --- |
| `full_context` | none; the whole log up to the checkpoint |
| `bm25` | none; BM25 over raw events, same context budget |
| `facts` | none; Strata facts and events |
| `eager` | incremental, dependency-tracked, rebuilt after every session |
| `full_rebuild` | every page rebuilt after every session (full consolidation); not answered, compared page by page with `eager` |
| `periodic` | full consolidation every 10 sessions, pages served in between |
| `untracked` | only pages of subjects named in the new session are rebuilt; no dependency tracking; pages served as current |
| `priority_15`, `priority_40` | a budget of 15% or 40% of `eager`'s mean page tokens per session, priority order; out-of-date pages withheld |
| `stale_priority_15` | `priority_15`, but out-of-date pages served as current |

The answerer is `composer-2.5` with the dataset's answer prompt.
The report gives accuracy over all cells (the pre-registered headline), splits by answerable and unanswerable gold, cells whose answer changed, a witnessed accuracy that also requires the cited event ids to match the gold support, and paired differences with 95% CIs from a bootstrap over questions, each drawn with all its checkpoints.

`stale_audit.py` compares each page a stale-serving system served with `eager`'s page for the same subject and checkpoint, and asks `grok-4.7-low` to count statements in the served page that the current page contradicts.
A blind check of 40 verdicts confirmed 6 of 20 positive verdicts for `untracked` and 5 of 10 for `stale_priority_15`, and none of the 10 negative verdicts flipped; most rejected positives were "no value recorded" statements where the current page has the value.
`results/stale_audit_blind_check.json` holds these 40 verdicts. The top-level README gives both rates.

## MEME

`meme.py` runs MEME (filler32k split, `data/meme_filler32k.json`).
Users state facts, some tied to another fact or given a successor value; later the other fact changes, and the questions check that the dependent fact changed (Cas), is reported as uncertain (Abs), was deleted (Del), is recalled (ER), is listed as a history (Tr) or aggregated (Agg).
One extraction pass per episode feeds `facts_plain`, `facts_annotated` (dependencies as text) and `strata` (dependencies as fact premises, successors as contingent writes); the baselines are `full_context` and `bm25`.
MEME's own judge prompts score the answers, including its trivial-pass rule.
The second-reader run answers the first 50 episodes with `grok-4.7-low` and judges them with `composer-2.5` (primary) and `grok-4.7-low`.

## Engine speed

`perf.py` measures ingest, page builds, a 1% update against a full rebuild, recall latency and storage on a seeded synthetic workload with no model:

```sh
PYTHONPATH=src python benchmarks/perf.py --sizes 1000 10000 100000 --output benchmarks/results/perf.json
```

Times depend on the machine and its load.

## Synthetic workload

`strata.bench` is a deterministic, seeded workload that compares maintenance strategies on cost and correctness without a model:

```sh
python -m strata.bench --seed 0                # prints stable JSON
python -m strata.bench --seed 0 --output result.json
strata bench --seed 0 --rounds 40              # same thing through the CLI
```

It generates 40 projects in 5 teams with 3 attributes each, then rounds of random updates and queries with a skewed read popularity, and replays the *same* schedule against a fresh database for each strategy:

| Strategy | What it does |
| --- | --- |
| `full_rebuild` | After every round's updates, mark every node stale and recompute all of them |
| `append_only_raw` | No derived memory; answers come from BM25 search over raw events |
| `fifo` | Per-round maintenance budget, FIFO order; unavailable nodes fall back to raw search |
| `priority` | Same budget, priority order |
| `lazy_fallback` | No background maintenance; each query repairs the nodes it needs within a per-query budget |
| `stale_control` | FIFO maintenance, but serves whatever is stored even if stale. Not a real strategy: it shows that the staleness metrics are not vacuous |

Queries are entity lookups (answered from a `profile` node) and team aggregates (the count of launched projects, answered from a level-2 `team` node).
Costs are abstract units declared per node (profile 1, team 3), **not wall-clock time**.
The JSON contains no timestamps, paths, or timings, and identical arguments produce byte-identical output (checked across processes and `PYTHONHASHSEED` values).
`append_only_raw` takes the top 3 BM25 hits for "`<entity> <attribute>`" and answers with the most recent hit about that entity and attribute; it cannot answer aggregates, so those count as unanswered.
The round budget is 6 units for `fifo`, `priority`, and `stale_control`; `lazy_fallback` may spend up to 8 units per query.

Default configuration, seed 0: 480 queries (333 lookups, 147 aggregates), 120 initial events, 320 updates, 40 rounds. Cost excludes the 55-unit initial build.

| Strategy | Maintenance cost | Lookup accuracy | Lookups served from raw fallback | Aggregates answered | Aggregate accuracy (of answered) | Stale served | Wrong derived answers served |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `full_rebuild` | 2200 | 1.000 | 0.000 | 1.000 | 1.000 | 0 | 0 |
| `append_only_raw` | 0 | 0.799 | 1.000 | 0.000 | n/a | 0 | 0 |
| `fifo` | 240 | 0.946 | 0.153 | 0.163 | 1.000 | 0 | 0 |
| `priority` | 238 | 0.982 | 0.072 | 0.204 | 1.000 | 0 | 0 |
| `lazy_fallback` | 453 | 1.000 | 0.000 | 1.000 | 1.000 | 0 | 0 |
| `stale_control` | 240 | 0.937 | 0.000 | 1.000 | 0.571 | 174 | 84 |

- `lazy_fallback` answered every query correctly for about 21% of `full_rebuild`'s maintenance cost (453 against 2200 units). `fifo` and `priority` cost about 11% of it, but answered fewer queries from derived memory, because their small budget leaves a backlog.
- Over seeds 0-4 (run once each), `lazy_fallback` lookup accuracy was 1.000 every time and its cost ranged from 403 to 463. `priority` had a lower lookup fallback rate than `fifo` every time (0.048-0.072 against 0.100-0.155).
- The safe strategies never served a stale or wrong derived value, while the control, which ignores node state, served 174 stale values, of which 84 were wrong.
- Same-value cutoff is visible in `full_rebuild`: 1548 of its 1800 recomputations produced unchanged output.
- This workload measures maintenance cost and answer correctness only. The real-data benchmarks above are the evidence on answer quality.
