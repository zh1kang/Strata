# MemDerive maintenance benchmark

Worlds: tripops_w1. Extraction and pages: `cursor:composer-2.5-fast`. Checkpoints at 20%, 40%, 60%, 80%, 100% of sessions; gold recompiled by the dataset oracle at each; deterministic scoring.

Extraction: 14 calls, 49,621 tokens, 294 facts, 0 sessions failed. Tokens are counted locally (o200k_base) over prompt and completion.

## Answers by `cursor:composer-2.5-fast`

| system | cells | accuracy | answerable | unanswerable | p100 | witnessed | refresh (changed) | explicit | implicit | value changes | old-value answers | stale abstentions | preservation | support recall | maint. tokens | query tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| eager | 529 | 59.4 | 73.1 | 31.8 | 63.6 | 49.1 | 56.8 | 55.7 | 61.5 | 73.7 | 10.5 | 8.1 | 84.5 | 88.1 | 641,305 | 4,304 |
| facts | 529 | 51.0 | 48.7 | 55.7 | 45.7 | 37.8 | 39.2 | 44.3 | 15.4 | 31.6 | 5.3 | 28.4 | 81.2 | 71.6 | 0 | 2,023 |
| untracked | 529 | 59.2 | 72.8 | 31.8 | 61.4 | 47.6 | 60.8 | 59.0 | 69.2 | 73.7 | 5.3 | 16.2 | 89.1 | 87.8 | 278,135 | 4,301 |
| priority_15 | 529 | 54.6 | 53.8 | 56.2 | 50.0 | 39.9 | 41.9 | 42.6 | 38.5 | 47.4 | 5.3 | 24.3 | 81.0 | 75.4 | 95,703 | 2,317 |
| stale_priority_15 | 529 | 57.7 | 62.9 | 47.2 | 55.0 | 44.0 | 47.3 | 45.9 | 53.8 | 57.9 | 10.5 | 18.9 | 86.2 | 82.1 | 95,703 | 3,311 |
| bm25 | 529 | 50.5 | 66.3 | 18.8 | 57.1 | 43.7 | 64.9 | 73.8 | 23.1 | 47.4 | 0.0 | 5.4 | 88.1 | 90.2 | 0 | 4,490 |
| full_context | 529 | 65.8 | 95.5 | 6.2 | 83.6 | 61.2 | 93.2 | 95.1 | 84.6 | 89.5 | 5.3 | 0.0 | 96.2 | 100.0 | 0 | 11,630 |
| lazy | 529 | 60.7 | 74.2 | 33.5 | 67.1 | 49.0 | 56.8 | 54.1 | 69.2 | 78.9 | 5.3 | 8.1 | 86.9 | 88.6 | 499,415 | 4,049 |

Answerable counts `answerable` and `closed_world_zero` gold; unanswerable is `unanswerable_incomplete`. Value changes are changed cells whose gold is a value both before and after (19 cells); old-value answers are the share of them answered with the previous value. Stale abstentions are changed cells answered `unanswerable_incomplete` as the previous gold was. The label split, value-change and stale-shown subsets were added after the tripops_w1 dev check; accuracy over all cells is the pre-registered headline.

Paired differences (accuracy points, 95% bootstrap CI over cells, which treats the five checkpoints of a question as independent):

- eager vs bm25: +8.9 [+4.3, +13.4] (n=529); changed -8.1 [-20.3, +4.1] (n=74); value_change +26.3 [+10.5, +47.4] (n=19); answerable +6.8 [+2.0, +11.9] (n=353); unanswerable +13.1 [+5.7, +20.5] (n=176)
- eager vs facts: +8.3 [+3.8, +12.7] (n=529); changed +17.6 [+5.4, +29.7] (n=74); value_change +42.1 [+21.1, +63.2] (n=19); answerable +24.4 [+19.5, +29.7] (n=353); unanswerable -23.9 [-30.7, -17.0] (n=176)
- eager vs full_context: -6.4 [-10.8, -1.9] (n=529); changed -36.5 [-47.3, -24.3] (n=74); value_change -15.8 [-36.8, +5.3] (n=19); answerable -22.4 [-27.2, -17.3] (n=353); unanswerable +25.6 [+19.3, +32.4] (n=176)
- eager vs untracked: +0.2 [-2.6, +3.2] (n=529); changed -4.1 [-13.5, +5.4] (n=74); value_change +0.0 [-21.1, +21.1] (n=19); answerable +0.3 [-3.1, +3.7] (n=353); unanswerable +0.0 [-5.1, +5.1] (n=176); stale_shown +0.2 [-2.7, +3.4] (n=474)
- lazy vs eager: +1.3 [-0.9, +3.8] (n=529); changed +0.0 [-6.8, +6.8] (n=74); value_change +5.3 [-10.5, +21.1] (n=19); answerable +1.1 [-1.7, +4.0] (n=353); unanswerable +1.7 [-2.8, +6.2] (n=176)
- priority_15 vs stale_priority_15: -3.0 [-6.0, +0.0] (n=529); changed -5.4 [-12.2, +0.0] (n=74); value_change -10.5 [-26.3, +0.0] (n=19); answerable -9.1 [-12.7, -5.7] (n=353); unanswerable +9.1 [+3.4, +15.3] (n=176); stale_shown -4.3 [-8.6, +0.0] (n=373)
- priority_15 vs facts: +3.6 [+0.8, +6.4] (n=529); changed +2.7 [-5.4, +10.8] (n=74); value_change +15.8 [+0.0, +36.8] (n=19); answerable +5.1 [+2.0, +8.2] (n=353); unanswerable +0.6 [-4.5, +5.7] (n=176)
- eager vs priority_15: +4.7 [+0.4, +9.1] (n=529); changed +14.9 [+4.1, +25.7] (n=74); value_change +26.3 [+10.5, +47.4] (n=19); answerable +19.3 [+14.7, +24.4] (n=353); unanswerable -24.4 [-31.8, -17.0] (n=176)
- facts vs bm25: +0.6 [-3.8, +5.1] (n=529); changed -25.7 [-36.5, -14.9] (n=74); value_change -15.8 [-31.6, +0.0] (n=19); answerable -17.6 [-21.8, -13.6] (n=353); unanswerable +36.9 [+29.0, +44.9] (n=176)

## Maintenance cost

| system | maint. tokens | wasted | page builds | live calls | pages | dirty pages at checkpoints | DB bytes |
|---|---|---|---|---|---|---|---|
| eager | 641,305 | 0 | 306 | 0 | 27 | 0.0 | 5,255,200 |
| facts | 0 | 0 | 0 | 0 | 0 | 0.0 | 4,607,600 |
| untracked | 278,135 | 0 | 138 | 0 | 27 | 14.6 | 5,144,512 |
| priority_15 | 95,703 | 0 | 46 | 0 | 27 | 18.2 | 4,927,496 |
| stale_priority_15 | 95,703 | 0 | 46 | 0 | 27 | 18.2 | 4,927,496 |
| full_rebuild | 2,767,370 | 0 | 1307 | 0 | 27 | 0.0 | 5,316,808 |
| bm25 | 0 | 0 | 0 | 0 | 0 | 0.0 | 4,521,584 |
| full_context | 0 | 0 | 0 | 0 | 0 | 0.0 | 4,521,584 |
| lazy | 499,415 | 0 | 234 | 0 | 27 | 4.8 | 5,222,552 |

## Incremental vs full rebuild

- tripops_w1: identical pages at 5/5 checkpoints, identical contexts 529/529; eager 641,305 vs rebuild 2,767,370 tokens (76.8% saved)
