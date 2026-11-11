# MemDerive maintenance benchmark

Worlds: tripops_w2, shopops_w2, eduops_w2, tripops_w3, tripops_w4, shopops_w3, eduops_w3. Extraction and pages: `cursor:composer-2.5-fast`. Checkpoints at 20%, 40%, 60%, 80%, 100% of sessions; gold recompiled by the dataset oracle at each; deterministic scoring.

Extraction: 105 calls, 373,920 tokens, 2,132 facts, 0 sessions failed. Tokens are counted locally (o200k_base) over prompt and completion.

## Answers by `cursor:composer-2.5`

| system | cells | accuracy | answerable | unanswerable | abstains (answerable) | p100 | witnessed | refresh (changed) | explicit | implicit | value changes | old-value answers | stale abstentions | preservation | support recall | maint. tokens | query tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| facts | 4267 | 58.3 | 62.7 | 51.7 | 27.0 | 56.1 | 41.6 | 47.5 | 48.4 | 43.8 | 45.7 | 15.2 | 22.6 | 88.4 | 78.7 | 0 | 2,075 |
| priority_15 | 4267 | 58.9 | 66.1 | 48.0 | 23.9 | 57.2 | 41.8 | 50.5 | 50.6 | 50.0 | 51.8 | 15.7 | 21.2 | 86.7 | 82.0 | 708,128 | 2,706 |
| priority_40 | 4267 | 58.8 | 68.1 | 44.6 | 21.5 | 57.7 | 43.3 | 51.7 | 51.9 | 50.8 | 56.3 | 13.7 | 21.5 | 84.6 | 84.9 | 1,896,359 | 3,433 |
| eager | 4267 | 61.5 | 74.6 | 41.7 | 17.0 | 62.8 | 46.8 | 58.1 | 58.8 | 55.5 | 61.9 | 8.1 | 19.3 | 86.2 | 89.8 | 4,762,508 | 4,085 |
| untracked | 4267 | 61.7 | 75.0 | 41.5 | 17.1 | 64.0 | 46.2 | 58.1 | 59.1 | 53.9 | 63.5 | 6.6 | 20.5 | 87.0 | 89.9 | 2,078,744 | 4,011 |

Answerable counts `answerable` and `closed_world_zero` gold; unanswerable is `unanswerable_incomplete`. Value changes are changed cells whose gold is a value both before and after (197 cells); old-value answers are the share of them answered with the previous value. Stale abstentions are changed cells answered `unanswerable_incomplete` as the previous gold was. Abstains (answerable) is the share of answerable cells answered `unanswerable_incomplete`. Witnessed counts a cell only if the answer is correct and its cited event ids match the gold support. The label split, value-change, stale-shown and witnessed comparisons and the abstention column were added after the tripops_w1 dev check; accuracy over all cells is the pre-registered headline.

Paired differences (accuracy points; 95% CI from a bootstrap over the questions of these worlds, each drawn with all its checkpoints, so it does not cover variation across worlds; n = cells; worlds k/m = k of m worlds have a difference with the same sign as the overall one, whatever that sign is):

- eager vs facts: +3.2 [+1.3, +5.2] (n=4267, worlds 5/7); changed +10.6 [+6.9, +14.3] (n=669, worlds 6/7); value_change +16.2 [+8.9, +23.5] (n=197, worlds 6/7); answerable +11.9 [+9.4, +14.7] (n=2570, worlds 7/7); unanswerable -10.0 [-12.3, -7.5] (n=1697, worlds 6/7); witnessed +5.2 [+3.3, +7.0] (n=4267, worlds 6/7)
- eager vs untracked: -0.1 [-1.2, +0.8] (n=4267, worlds 2/7); changed +0.0 [-2.3, +2.4] (n=669, worlds 1/7); value_change -1.5 [-5.9, +3.0] (n=197, worlds 4/7); answerable -0.4 [-1.7, +1.0] (n=2570, worlds 3/7); unanswerable +0.2 [-1.3, +1.7] (n=1697, worlds 4/7); stale_shown -0.2 [-1.4, +1.0] (n=3519, worlds 3/7); witnessed +0.5 [-0.5, +1.6] (n=4267, worlds 5/7)
- priority_15 vs facts: +0.5 [-0.5, +1.6] (n=4267, worlds 4/7); changed +3.0 [+0.7, +5.3] (n=669, worlds 4/7); value_change +6.1 [+1.9, +10.4] (n=197, worlds 6/7); answerable +3.3 [+2.1, +4.8] (n=2570, worlds 7/7); unanswerable -3.7 [-5.5, -2.0] (n=1697, worlds 7/7); witnessed +0.3 [-0.8, +1.3] (n=4267, worlds 3/7)
- eager vs priority_15: +2.7 [+1.0, +4.4] (n=4267, worlds 6/7); changed +7.6 [+4.1, +11.1] (n=669, worlds 6/7); value_change +10.2 [+3.0, +17.3] (n=197, worlds 6/7); answerable +8.6 [+6.2, +10.9] (n=2570, worlds 7/7); unanswerable -6.2 [-8.4, -3.9] (n=1697, worlds 6/7); witnessed +4.9 [+3.3, +6.6] (n=4267, worlds 7/7)
- eager vs priority_40: +2.8 [+1.5, +4.0] (n=4267, worlds 7/7); changed +6.4 [+3.6, +9.5] (n=669, worlds 5/7); value_change +5.6 [+0.0, +11.1] (n=197, worlds 5/7); answerable +6.5 [+4.7, +8.3] (n=2570, worlds 7/7); unanswerable -2.9 [-4.5, -1.2] (n=1697, worlds 5/7); witnessed +3.4 [+2.3, +4.6] (n=4267, worlds 6/7)
- priority_40 vs untracked: -2.9 [-4.3, -1.5] (n=4267, worlds 7/7); changed -6.4 [-9.2, -3.6] (n=669, worlds 6/7); value_change -7.1 [-12.0, -2.0] (n=197, worlds 5/7); answerable -6.9 [-8.8, -5.1] (n=2570, worlds 7/7); unanswerable +3.1 [+1.2, +5.0] (n=1697, worlds 5/7); stale_shown -3.5 [-5.1, -1.9] (n=3519, worlds 7/7); witnessed -2.9 [-4.2, -1.6] (n=4267, worlds 7/7)

## Maintenance cost

| system | maint. tokens | wasted | page builds | pages | dirty pages at checkpoints | DB bytes |
|---|---|---|---|---|---|---|
| facts | 0 | 0 | 0 | 0 | 0.0 | 4,685,606 |
| priority_15 | 708,128 | 0 | 356 | 183 | 16.9 | 4,959,056 |
| priority_40 | 1,896,359 | 0 | 988 | 183 | 10.8 | 5,142,853 |
| eager | 4,762,508 | 0 | 2316 | 183 | 0.0 | 5,373,133 |
| periodic | 1,954,791 | 0 | 975 | 183 | 12.5 | 5,257,024 |
| untracked | 2,078,744 | 0 | 1014 | 183 | 14.7 | 5,221,761 |
| full_rebuild | 20,045,634 | 0 | 10092 | 183 | 0.0 | 5,425,871 |

## Incremental vs full rebuild

- tripops_w2: identical pages at 5/5 checkpoints, identical contexts 537/537; eager 541,708 vs rebuild 2,969,542 tokens (81.8% saved)
- shopops_w2: identical pages at 5/5 checkpoints, identical contexts 559/559; eager 442,144 vs rebuild 1,702,697 tokens (74.0% saved)
- eduops_w2: identical pages at 5/5 checkpoints, identical contexts 750/750; eager 951,261 vs rebuild 4,369,850 tokens (78.2% saved)
- tripops_w3: identical pages at 5/5 checkpoints, identical contexts 543/543; eager 632,329 vs rebuild 2,489,265 tokens (74.6% saved)
- tripops_w4: identical pages at 5/5 checkpoints, identical contexts 525/525; eager 537,987 vs rebuild 2,479,335 tokens (78.3% saved)
- shopops_w3: identical pages at 5/5 checkpoints, identical contexts 577/577; eager 657,703 vs rebuild 2,004,813 tokens (67.2% saved)
- eduops_w3: identical pages at 5/5 checkpoints, identical contexts 776/776; eager 999,376 vs rebuild 4,030,132 tokens (75.2% saved)
