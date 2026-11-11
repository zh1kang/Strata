# MEME (filler32k) results

50 episodes. Extraction by `cursor:composer-2.5-fast`: 1150 calls, 1,688,308 tokens, 4843 facts (233 with premises), 95 successor rules, 0 failed sessions, 0 rejected writes. Gold dependency edges recorded: 84.5% of 155.

Cas/Abs/Del count a pass only if the same entity's before-question passed (MEME's trivial-pass rule); raw judge pass rates in parentheses.

## Answers by `cursor:grok-4.7-low`, judged by `cursor:composer-2.5`

| system | after | task mean | ER | Agg | Tr | Del | Cas | Abs | Cas 2-hop | query tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| full_context | 65.4 | 67.9 | 100.0 | 94.0 | 78.0 | 76.0 (78.0) | 57.5 (60.3) | 1.7 (1.7) | 47.8 | 33,319 |
| strata | 69.0 | 69.8 | 100.0 | 94.0 | 68.0 | 64.0 (68.0) | 79.5 (82.2) | 13.6 (13.6) | 82.6 | 2,229 |

Paired differences on after-questions (points, 95% CI from a bootstrap over episodes):

- strata vs full_context: +3.6 [-1.5, +8.4] over 332 questions

## Answers by `cursor:grok-4.7-low`, judged by `cursor:grok-4.7-low`

| system | after | task mean | ER | Agg | Tr | Del | Cas | Abs | Cas 2-hop | query tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| full_context | 60.8 | 63.1 | 100.0 | 92.0 | 52.0 | 78.0 (82.0) | 54.8 (58.9) | 1.7 (1.7) | 43.5 | 33,319 |
| strata | 68.4 | 69.6 | 100.0 | 94.0 | 54.0 | 82.0 (92.0) | 74.0 (78.1) | 13.6 (13.6) | 73.9 | 2,229 |

Paired differences on after-questions (points, 95% CI from a bootstrap over episodes):

- strata vs full_context: +7.5 [+3.4, +11.4] over 332 questions

Judge agreement on `cursor:grok-4.7-low` answers: 96.1% of 1128 verdicts.
