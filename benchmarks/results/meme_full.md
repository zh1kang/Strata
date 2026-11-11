# MEME (filler32k) results

100 episodes. Extraction by `cursor:composer-2.5-fast`: 2145 calls, 2,672,851 tokens, 7140 facts (470 with premises), 214 successor rules, 0 failed sessions, 0 rejected writes. Gold dependency edges recorded: 86.0% of 358.

Cas/Abs/Del count a pass only if the same entity's before-question passed (MEME's trivial-pass rule); raw judge pass rates in parentheses.

## Answers by `cursor:composer-2.5-fast`, judged by `cursor:grok-4.7-low-fast`

| system | after | task mean | ER | Agg | Tr | Del | Cas | Abs | Cas 2-hop | query tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| full_context | 44.8 | 49.0 | 100.0 | 73.0 | 81.0 | 13.0 (15.0) | 26.8 (28.0) | 0.0 (0.8) | 15.6 | 33,365 |
| bm25 | 28.4 | 31.7 | 100.0 | 8.0 | 52.0 | 20.0 (22.0) | 10.4 (11.6) | 0.0 (4.6) | 9.4 | 6,289 |
| facts_plain | 51.0 | 58.9 | 100.0 | 89.0 | 75.0 | 88.0 (92.0) | 0.6 (0.6) | 0.8 (1.5) | 1.6 | 1,608 |
| facts_annotated | 65.6 | 68.8 | 100.0 | 88.0 | 70.0 | 88.0 (91.0) | 65.9 (69.5) | 0.8 (2.3) | 70.3 | 1,775 |
| strata | 67.1 | 70.1 | 100.0 | 83.0 | 76.0 | 90.0 (93.0) | 70.1 (70.7) | 1.5 (2.3) | 71.9 | 1,676 |

Paired differences on after-questions (points, 95% bootstrap CI):

- strata vs facts_plain: +16.1 [+13.0, +19.2] over 694 questions
- strata vs facts_annotated: +1.6 [-0.1, +3.5] over 694 questions
- strata vs bm25: +38.8 [+35.0, +42.7] over 694 questions
- strata vs full_context: +22.3 [+18.6, +25.9] over 694 questions
- facts_annotated vs facts_plain: +14.6 [+11.5, +17.7] over 694 questions

