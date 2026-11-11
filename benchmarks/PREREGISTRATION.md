# Pre-registration: held-out MemDerive and MEME second-reader runs

Written 2026-11-10, before any held-out MemDerive answer with `cursor:composer-2.5` and before any MEME answer with `cursor:grok-4.7-low`.
An earlier held-out run with `cursor:composer-2.5-fast` was stopped after about 650 answers on four worlds; those answers stay in the response cache but were never written to an answers file or scored.
`scripts/run_final.sh` prints the SHA-256 of this file and of every benchmark script when it starts, so the versions used are on record.

## Setup

- MemDerive held-out worlds: `tripops_w2`, `shopops_w2`, `eduops_w2`, `tripops_w3`, `tripops_w4`, `shopops_w3`, `eduops_w3` (4,267 cells).
- Answerer: `cursor:composer-2.5`.
- Fact extraction and page writing: `cursor:composer-2.5-fast`, already complete for every world; the run does not call `extract` or `simulate`.
- MEME second reader: `cursor:grok-4.7-low` answers the first 50 episodes for `full_context` and `strata`; `cursor:composer-2.5` is the primary judge and `cursor:grok-4.7-low` the secondary judge.
- Confidence intervals: 95% percentile bootstrap, 2,000 rounds, seed 0, resampling clusters: (world, question) with all its checkpoints for MemDerive, episodes for MEME.
- A difference counts as shown only if its interval excludes 0.

## Claims and primary metrics

| Claim | Primary metric |
|---|---|
| C1. Incremental, dependency-tracked maintenance equals a full rebuild for less compute | Identical pages at every checkpoint (35/35 held-out, already measured) and maintenance tokens saved against `full_rebuild` |
| C2. Structured memory improves answers | `eager - facts`, accuracy over all cells |
| C3a. A budgeted schedule degrades gracefully | `priority_15 - facts`, accuracy over all cells; companion: `eager - priority_15` and the maintenance-token ratio `priority_15 / eager` |
| C3b. Withholding out-of-date pages | `priority_15 - stale_priority_15` on `stale_shown` cells; staleness audit share of served pages with a wrong statement |
| C4. Dependency tracking against the `untracked` heuristic | `eager - untracked` on `changed` cells; staleness audit share of served pages with a wrong statement |
| C5. MEME against full context with a second reader | `strata - full_context` on after-questions, grok answers, composer judge |

## Secondary and descriptive analyses

- Answerable and unanswerable accuracy, value-change refresh, old-value answers and stale abstentions. These splits were added after the `tripops_w1` dev check, which showed that withholding mainly changes abstention.
- `bm25` and `full_context` comparisons on MemDerive, with query tokens.
- Maintenance cost rows for `periodic` and `untracked`.
- `priority_40` (tag `heldout_p40`): a third budget point for the accuracy-against-cost chart; descriptive only.

## Staleness audit

- Script: `stale_audit.py`. Judge: `cursor:grok-4.7-low`. Reference: `eager`'s page for the same subject at the same checkpoint, which is always current.
- A served page with the same text as the reference is current. Differing pages are judged for statements the reference contradicts, and separately for facts they lack.
- `untracked`: a seed-0 sample of 250 of its 510 differing pages. `stale_priority_15`: all 142 differing pages (a census).
- Metric: the share of differing pages with at least one contradicted statement, with a Wilson 95% interval, and that share scaled to all served pages.
- Pairs repeat pages across checkpoints (510 pairs from 163 pages; 142 from 53), so the Wilson interval overstates independence.

## Known from development data

On `tripops_w1`: `eager` 59.4, `untracked` 59.2, `priority_15` 54.6, `stale_priority_15` 57.7, `facts` 51.0, `full_context` 65.8.
Withholding raised accuracy on unanswerable cells and lowered it on answerable ones.
On MEME, the same 50 episodes with `cursor:composer-2.5-fast` answers and the `grok-4.7-low-fast` judge give `strata - full_context` +18.7 [+13.9, +23.4] (+22.3 on all 100 episodes); a 4-episode smoke run with grok-fast answers gave +12 and +16, depending on the judge.
The held-out results are reported whatever their direction.
