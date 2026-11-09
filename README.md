# Strata

Strata is a small, dependency-light SQLite memory engine for agents (Python 3.12+, standard library only).
It keeps an append-only log of dated events, versioned fact slots (single- or set-valued), and *derived nodes* (summaries, profiles, topic digests, anything computed from facts or sessions) whose inputs are tracked exactly.
When a fact changes, only the derived nodes that actually read it are invalidated, repaired within a cost budget, and never served while out of date.

The core is model-free. Language-model support lives in a separate optional boundary, `strata.llm`, which the core never imports.

## Quick start

```python
from strata import Budget, Event, Memory

with Memory.open("agent.db") as m:
    m.ingest("s1", [Event("s1:1", "s1", "message", {"text": "Atlas launches Dec 10"})])
    m.assert_fact("atlas", "launch_date", "2026-12-10", source_key="s1:1")

    # A derived node: level 1, reads facts through a tracked context.
    m.define("summary", "atlas", 1, lambda ctx: {"launch": ctx.slot("atlas", "launch_date")})
    # A level-2 node reading the level-1 node.
    m.define("banner", "atlas", 2, lambda ctx: f"Launch: {ctx.node('summary', 'atlas')['launch']}")

    m.maintain(Budget(10))                  # builds both nodes
    print(m.recall("launch").items)         # current facts and nodes, each with provenance

    m.ingest("s1", [Event("s1:2", "s1", "message", {"text": "Atlas moved to Jan 15"})])
    m.assert_fact("atlas", "launch_date", "2027-01-15", "s1:2")
    r = m.recall("launch")                  # summary/banner are withheld; falls back to raw events
    print(r.suppressed, r.fallback_used)    # 2 True
    m.maintain(Budget(10))                  # repairs summary, then banner
```

The same is available from the shell (every command prints JSON; `--db` or `STRATA_DB` selects the file):

```sh
strata --db agent.db ingest --session s1 --key s1:1 --text "Atlas launches Dec 10"
strata --db agent.db assert atlas launch_date '"2026-12-10"' --source s1:1
strata --db agent.db define-profile atlas      # built-in node holding all facts of a subject
strata --db agent.db define-topic launches     # built-in node holding the facts filed under a topic
strata --db agent.db maintain --budget 5 --policy priority
strata --db agent.db recall launch
strata --db agent.db explain profile atlas     # dependencies, dependents, provenance
```

Without installing, use `PYTHONPATH=src python -m strata ...`. Install with `pip install -e '.[dev]'` to get the `strata` and `strata-bench` commands.

### CLI commands

`init`, `status`, `ingest` (`--text`, `--payload JSON`, or `--file events.jsonl`; `--at` for the event time), `assert` (`--expect-version` for compare-and-set, `--mode set|add|remove`), `retract`, `facts`, `history`, `define-profile`, `define-topic`, `maintain` (`--budget`, `--policy fifo|priority`), `rebuild`, `retry-failed`, `recall` (`--limit`, `--include-stale`, `--no-fallback`, `--lazy-budget`, `--events`, `--since`, `--until`), `nodes`, `explain`, `bench`.
Errors print `error: ...` to stderr and exit with status 1.

The CLI can only maintain the built-in `profile` and `topic` derivations, because Python derivation functions are not persisted (see Limitations).

## Public API

| Area | API |
| --- | --- |
| Events | `ingest(session_id, events)`, `event(key)`, `events(session_id, limit)`; `Event(key, session_id, kind, payload, at=None)` |
| Facts | `assert_fact`, `assert_facts` (atomic batch of `FactWrite`, with `mode` set/add/remove/retract), `retract_fact`, `get_fact`, `facts(subject)`, `fact_history`, `head_version` |
| Derived nodes | `define(kind, key, level, fn, cost, version, search_text, keywords)`, `node`, `nodes(state)`, `dependencies`, `dependents`, `explain` |
| Maintenance | `maintain(budget, policy)` returning a `MaintenanceReport`, `rebuild`, `retry_failed` |
| Retrieval | `recall(query, include_stale, fallback, lazy_budget, limit, events, since, until)`, `read_node(kind, key, refresh_budget)`, `search_events(query, limit, since, until)`, `strata.index.snippet(text, query, width)` |
| Introspection | `status()` |

A derivation is a function `fn(ctx)` that returns a JSON-serializable value. It may read only through `ctx`:

- `ctx.slot(subject, attribute, default=None)` reads one fact slot. Missing and retracted slots return `default` and are still tracked.
- `ctx.subject(subject)` reads every live attribute of a subject.
- `ctx.history(subject, attribute)` reads a slot's value changes, oldest first, each with its source event key and date. It is tracked like `ctx.slot`; re-asserting the current value is not a change.
- `ctx.session(session_id)` reads every event of a session in arrival order. It is tracked: appending an event to that session invalidates the reader, and nothing else does.
- `ctx.event(key)` reads an event payload. Events are immutable, so this read needs no tracking.
- `ctx.node(kind, key)` reads a node of a *strictly lower* level that is currently valid.
- `ctx.charge(cost)` reports what this run really cost (for example model tokens). From then on the scheduler uses the node's latest measured cost instead of its declared `cost`.

A node is searchable by its kind, key, and `keywords` from the moment it is defined, so a query can find, withhold, or lazily build a node that was never built. Once built it is also indexed by `search_text(value)` if given, otherwise by its flattened value. Defining a node again with different keywords re-indexes it without invalidating it; this is how a lazily maintained node stays findable as what it covers grows. `define_profile` indexes profiles by attribute names only, because a profile holds every value of its subject.

`expected_version` on a fact write is a compare-and-set on the slot head: `None` skips the check, `0` means "the slot must not exist yet", and any other value must equal the current head version id.

`FactWrite.mode` makes a slot set-valued: `add` appends the given item(s) that are not already present (a scalar head counts as a one-item list), `remove` deletes them, and `retract` retracts the slot. These read the head inside the write transaction, so concurrent additions never lose an item, and `remove`/`retract` of something absent write nothing instead of failing the batch. Adding an item that is already present is a same-value write and invalidates nothing.

`Event.at` is when an event happened (ISO 8601, normalised on ingest; part of the event's identity). Facts, history entries, and recalled items carry the date of their source event as `at`.

Subjects that start with `_` are internal bookkeeping: tracked like any slot but never indexed for search. The built-in topic nodes use this: `define_topic(memory, name)` defines a node holding every live fact listed in the set-valued slot `_topic:<name>.members` (pairs of subject and attribute). It is invalidated when one of those facts changes or a new one joins the topic, and by nothing else. `strata.llm.apply_extraction` maintains the membership from the `topic` field of extracted facts.

## Architecture

```
events.py      append-only event log, idempotent ingest
facts.py       versioned fact slots, CAS, retraction, same-value cutoff
graph.py       dependency graph: invalidation, verification, provenance
context.py     tracked reads handed to derivations
maintenance.py node definitions, leases, optimistic rebuilds, failure handling
scheduler.py   FIFO and priority policies
retrieval.py   recall with provenance, stale suppression, raw fallback, lazy refresh
index.py       incremental FTS5 index (Porter stemming, stopwords, fact-expanded events), snippets
profiles.py    built-in profile and topic derivations
store.py       SQLite connection, write transactions, schema version check and migration
llm/           optional model boundary (Model protocol, fake model, cache, extraction)
bench.py       deterministic seeded benchmark
cli.py         command line
```

Storage is one SQLite file in WAL mode. Events and fact versions are protected by triggers that reject `UPDATE` and `DELETE`.
Every committed write is stamped with a monotonically increasing revision; a transaction that writes nothing (for example, re-ingesting only duplicates) consumes no revision.

### How invalidation works

Each node stores the inputs it observed, with the revision of each: fact slots, whole subjects, sessions, and lower-level nodes.

- A slot's `changed_revision` only advances when its *effective value* changes. Re-asserting an identical value records the new source event for provenance but invalidates nothing.
- Reading a slot that does not exist yet records a dependency on that slot key. Reading a subject records a dependency on the subject as a whole. Creating the slot later therefore invalidates the reader (phantom dependencies).
- When facts change, direct readers become `stale` and all transitive dependents (found with a recursive query) become `suspect`.
- A `suspect` node whose recorded inputs all still match is re-validated at zero cost, without running its function. A node whose recomputed output is byte-identical (canonical JSON hash) keeps its `changed_revision`, so nothing downstream of it is recomputed (same-value cutoff).
- Changing a node's `version` string (its code version) invalidates it and its dependents.

### Rebuilds are optimistic and leased

A rebuild claims a time-limited lease on the node, runs the derivation outside any write transaction, then commits in one `BEGIN IMMEDIATE` transaction that re-checks the lease token and that every observed input revision (and node validity) still matches.
If an input moved, or the lease was lost, the result is discarded, counted as `conflicts` and `wasted_cost`, and the node stays dirty. A stale result is never committed as valid.
A derivation that raises is retried on later runs; after `max_attempts` (default 3) the node becomes `failed`, is withheld, and stays parked until `retry_failed()` or until an input changes. `LevelViolationError` is a programming error: the node is parked and the exception propagates.
A node that reads a lower-level node that is not yet valid is deferred at no cost and retried after progress. In a restricted run (`lazy_budget`, `refresh_budget`) the missing node joins the run, so a chain of nodes that were never built can be built on demand.

### Scheduling

`maintain(budget, policy)` considers dirty nodes whose node dependencies are all valid, in a deterministic total order, and spends `cost` units per recomputation: the node's latest measured cost if its derivation calls `ctx.charge`, otherwise its declared cost. Eligible nodes are kept in a priority queue and only the dependents of a node that became valid are re-examined, so a run over `n` independent nodes takes time linear in `n` (8,000 nodes: 4.0 s, previously 72.6 s with a full rescan per job), with the same choices a full rescan would make.

- `fifo`: oldest dirty first, ties by node id.
- `priority`: highest `(1 + access_count) / cost` first (frequently read, cheap nodes), ties by age then node id. `recall` and `read_node` record accesses, including for nodes that were withheld.
- Budget is first-fit in policy order: a node that does not fit in the remaining budget is skipped and reported (`skipped_budget`), and later nodes that fit still run. With declared costs the budget is never exceeded; a measured cost is only known after a run, so a run can overshoot by one job's misestimate. `Budget(cost, max_jobs)` can also cap job count.
- Zero-cost verification of suspect nodes always runs, even with a zero budget.

`MaintenanceReport` fields: `jobs`, `verified`, `cutoffs`, `cost`, `wasted_cost`, `conflicts`, `failures`, `lease_busy`, `not_ready`, `unregistered`, `skipped_budget`, `dirty`, `write_amplification` (node values actually rewritten), and per-node `outcomes`.

### Retrieval

`recall(query)` searches current fact heads and derived nodes with FTS5 (BM25) and resolves status against live tables at query time.
Words are Porter-stemmed and English function words are dropped from queries (unless the query has nothing else). An event's search document also holds the text of every fact extracted from it, so a question phrased like a fact finds the conversation that stated it; the returned item still shows the event as it was said.

- A derived node is returned as `current` only while its state is `valid`. Stale, suspect, pending, and failed nodes are withheld and counted in `suppressed`.
- `include_stale=True` returns withheld nodes explicitly labelled `stale`.
- If nothing current matched, or a relevant node was withheld, raw event matches are appended with status `raw` and a warning that they are unverified evidence (`fallback_used`). `fallback=False` disables this.
- `events=N` always appends up to `N` raw events after current memory: the source event of each returned fact, interleaved with plain event search hits, without duplicates. Facts say what is current; their source events keep the detail and the dates that a one-line fact drops. On LongMemEval, recall without source events lost 20-28 points of accuracy to plain event search (see `benchmarks/README.md`).
- `lazy_budget=N` lets the query first repair, within `N` cost units, the dirty nodes it matched (and their dirty upstream nodes, including ones never built); whatever is still dirty is withheld.
- `since` / `until` (ISO 8601, `[since, until)`) keep only events dated inside the window, and facts whose source event is; the window is applied inside the search, so it does not just filter a truncated list. Derived nodes are not filtered.
- `strata.index.snippet(text, query, width)` cuts long text to the sentences that match a query, widened with their neighbours, with `…` at every gap; useful for fitting more retrieved items into a model's context.
- Every item carries `provenance`: `fact:<subject>/<attribute>@<version>`, `event:<key>`, and `node:<kind>/<key>` entries, derived from recorded dependencies rather than anything a model asserted.
- Free-text queries are tokenized and quoted, so punctuation and FTS operators in a query cannot cause syntax errors.

## Invariants

1. Events and fact versions are immutable and append-only. Re-delivering an event with the same key and content is a no-op; the same key with different content is rejected.
2. A node in state `valid` has every node dependency `valid` and every observed input revision equal to the current one.
3. Only `valid` nodes are returned as `current`; withheld items are never silently included.
4. A rebuild commits only if its lease is still held and every input it read is unchanged at commit time.
5. Derivations may only read strictly lower-level nodes, so the dependency graph is acyclic.
6. A fact write batch is atomic: compare-and-set failure or an unknown source event rolls back the whole batch.
7. Scheduling is deterministic: the same database state and arguments give the same order and the same report.
8. Invalidation depends only on reads made through `Context`, never on citations or claims from a model.

## LLM integration boundary

`strata.llm` is optional and independent of the core. To support a real provider, implement the `Model` protocol:

```python
from strata.llm import Model, ModelRequest, ModelResponse

class MyAdapter:
    name = "my-provider/my-model"

    def complete(self, request: ModelRequest) -> ModelResponse:
        text = call_my_provider(request.system, request.prompt, request.temperature)
        return ModelResponse(text=text, model=self.name)
```

Provided pieces:

- `FakeModel`: scripted replies (mapping from prompt substring, ordered list, or callable) with a recorded call log, for tests and offline use.
- `CachedModel(model, cache)` with `InMemoryCache` or `SqliteCache(path)`: caches deterministic (`temperature == 0`) requests under a key covering model name, system prompt, prompt, temperature, and token limit.
- `parse_extraction` / `extract_facts` / `extract_and_assert`: ask a model for `{"facts": [{"subject", "attribute", "value", "op", "topic", "evidence", "confidence"}]}` and validate strictly. `op` is `set` (default), `add`, `remove`, or `retract` and becomes the write's `mode`; `topic` files the fact under a topic node. Several `add`s to one slot in one reply are allowed; two different `set`s are a conflict. Validation is all-or-nothing: invalid JSON, wrong shape, unknown fields, null values, non-finite numbers, out-of-range or non-numeric confidence, conflicting duplicate facts, too many facts, oversized values, or (when a source text is given) evidence that does not occur in the source event all raise `ExtractionError`, and nothing is written. A reply wrapped in a single markdown code fence is accepted; any other surrounding text is not.
- Accepted facts are asserted atomically with the source event as provenance. Low-confidence facts can be dropped with `min_confidence`.

No provider adapter is shipped, and no model is ever called by the core.

## Benchmark

```sh
python -m strata.bench --seed 0                # prints stable JSON
python -m strata.bench --seed 0 --output result.json
strata bench --seed 0 --rounds 40              # same thing through the CLI
```

The benchmark generates a synthetic workload from the seed (40 projects in 5 teams, 3 attributes each, then rounds of random updates and queries with a skewed read popularity) and replays the *same* schedule against a fresh database for each strategy:

| Strategy | What it does |
| --- | --- |
| `full_rebuild` | After every round's updates, mark every node stale and recompute all of them |
| `append_only_raw` | No derived memory; answers come from BM25 search over raw events |
| `fifo` | Per-round maintenance budget, FIFO order; unavailable nodes fall back to raw search |
| `priority` | Same budget, priority order |
| `lazy_fallback` | No background maintenance; each query repairs the nodes it needs within a per-query budget |
| `stale_control` | FIFO maintenance, but serves whatever is stored even if stale. Not a real strategy: it shows that the staleness metrics are not vacuous |

Queries are entity lookups (answered from a `profile` node) and team aggregates (count of launched projects, answered from a level-2 `team` node). Costs are abstract units declared per node (profile 1, team 3), **not wall-clock time**. The JSON contains no timestamps, paths, or timings, and identical arguments produce byte-identical output (checked across processes and `PYTHONHASHSEED` values).

Baseline definitions matter for reading the numbers: `append_only_raw` takes the top 3 BM25 hits for "`<entity> <attribute>`" and answers with the most recent hit that is about that entity and attribute. It cannot answer aggregates, so those count as unanswered. Round budget is 6 units for `fifo`, `priority`, and `stale_control`; `lazy_fallback` may spend up to 8 units per query.

### Measured results (default config, seed 0)

480 queries (333 lookups, 147 aggregates), 120 initial events, 320 updates, 40 rounds. Cost excludes the 55-unit initial build.

| Strategy | Maintenance cost | Lookup accuracy | Lookups served from raw fallback | Aggregates answered | Aggregate accuracy (of answered) | Stale served | Wrong derived answers served |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `full_rebuild` | 2200 | 1.000 | 0.000 | 1.000 | 1.000 | 0 | 0 |
| `append_only_raw` | 0 | 0.799 | 1.000 | 0.000 | n/a | 0 | 0 |
| `fifo` | 240 | 0.946 | 0.153 | 0.163 | 1.000 | 0 | 0 |
| `priority` | 238 | 0.982 | 0.072 | 0.204 | 1.000 | 0 | 0 |
| `lazy_fallback` | 453 | 1.000 | 0.000 | 1.000 | 1.000 | 0 | 0 |
| `stale_control` | 240 | 0.937 | 0.000 | 1.000 | 0.571 | 174 | 84 |

What this does and does not show:

- On this workload, `lazy_fallback` answered every query correctly for about 21% of `full_rebuild`'s maintenance cost (453 vs 2200 units), and `fifo`/`priority` cost about 11% of it while answering fewer queries from derived memory because their small budget leaves a backlog. Over seeds 0-4 (run once each) `lazy_fallback` lookup accuracy was 1.000 every time, its cost ranged 403-463, and `priority` had a lower lookup fallback rate than `fifo` every time (0.048-0.072 vs 0.100-0.155). Those ranges are specific to this generator and these parameters.
- The safe strategies never served a stale or wrong derived value (`Stale served` and `Wrong derived answers served` are 0), while the control, which ignores node state, served 174 stale values of which 84 were wrong.
- Same-value cutoff is visible in `full_rebuild`: 1548 of its 1800 recomputations produced unchanged output.
- This is a synthetic benchmark of maintenance cost and answer correctness. It is not evidence that derived memory beats raw search or embedding retrieval on real agent workloads, and the raw baseline is simple lexical search. Results depend on the budgets chosen.

## Development

```sh
pip install -e '.[dev]'
python -m pytest
python -m ruff check src tests
python -m compileall -q src tests
```

## Limitations

- **Derivations are not persisted.** Functions must be registered with `define` in every process. Nodes whose function is not registered are skipped and reported as `unregistered`. The CLI only knows the built-in `profile` derivation.
- **Derivations must be deterministic functions of their tracked reads.** Anything read outside `ctx` (clock, files, network, global state) is untracked and will not trigger invalidation. A derivation that calls a model is deterministic only up to the model (cache its responses); Strata tracks what it read, not what the model did with it. `ctx.event(key)` is untracked because events never change; read a whole session with `ctx.session` to be invalidated by new events.
- **Slots are versioned values, not a temporal database.** Set-valued slots and event dates are supported, but there is no valid-time modelling (when a fact *became* true, as opposed to when it was stated) and no contradiction inference beyond replacing a slot's value.
- **Concurrency is coarse.** A `Memory` is not thread-safe; use one per thread or process. SQLite serialises writers. Leases are time-based without heartbeat or renewal, so a derivation that outlives `lease_ttl` can lose its lease and have its work discarded; clock skew between machines is not handled. Nothing here is distributed.
- **Scheduler trade-offs.** First-fit budgeting never exceeds the budget but can skip an expensive node indefinitely (a node costing more than the whole budget never runs). The priority policy can starve rarely read nodes while hot nodes keep being dirtied. Measured costs (`ctx.charge`) are only as good as the derivation's own accounting.
- **Search is lexical.** FTS5/BM25 with Porter stemming; no embeddings or synonyms. Time windows need dated events. A withheld node's previous text stays indexed so it can be located, but is never returned as current. Each recall records node accesses with a small write.
- **Data lifecycle.** Events and fact versions cannot be deleted or edited, so there is no erasure mechanism. Search results are capped by `limit`.
- **Schema.** The schema is versioned (`3`). A version-2 database is migrated in place on open (dates, sessions, stemmed index, fact-expanded events); any other version raises `SchemaError`.
- **LLM boundary.** No provider adapters are included. Extraction validation guards the format and (optionally) grounding of model output but cannot judge whether an extracted fact is true.
- **Benchmark.** Synthetic, single-process, and sensitive to its parameters and baseline definitions, as described above.
