# LongMemEval-S results: v3

Dataset: LongMemEval-S cleaned (xiaowu0162/longmemeval-cleaned, longmemeval_s_cleaned.json), sha256 `d6f21ea9d60a0d56`.
Questions: 500 (30 abstention). By type: {'multi-session': 133, 'temporal-reasoning': 133, 'knowledge-update': 78, 'single-session-user': 70, 'single-session-assistant': 56, 'single-session-preference': 30}.
Models: extraction `cursor:composer-2.5-fast`, judge `cursor:cursor-grok-4.6-low-fast` (secondary judge `cursor:composer-2.5-fast`), answerers ['cursor:composer-2.5-fast'].
Config: {'context_items': 10, 'strata_recall_events': 20, 'raw_search_item_chars': 3000, 'snippet_chars': 1500, 'context_chars': 24000, 'window_events': 10, 'derived_nodes': 'topic per extracted topic, timeline per changed single-valued fact', 'answer_prompt': 'note-taking (Chain-of-Note); judges see only the final ANSWER line', 'background_budget_per_session': 1.0, 'lazy_budget_per_query': 8.0, 'digest_model': 'cursor:composer-2.5-fast', 'digest_background_tokens_per_session': 1500.0, 'digest_lazy_tokens_per_query': 6000.0}.
Extraction: {'questions': 500, 'calls': 4048, 'in_tokens': 11311533, 'out_tokens': 14192824, 'failed_sessions': 241, 'sessions': 23867, 'facts': 67860}.

## Answer model `cursor:composer-2.5-fast`

| system | n | accuracy | task-avg | accuracy (judge2) | judge agreement | KU acc | KU strict current-only | KU outdated-as-current | abs-question acc | abstain on answerable | diff vs raw_search (95% CI) | diff vs raw_search_tuned (95% CI) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raw_search | 500 | 0.786 (393/500) | 0.800 | 0.712 (356/500) | 0.910 (455/500) | 0.931 (67/72) | 0.597 (43/72) | 0.028 (2/72) | 0.933 (28/30) | 0.126 (59/470) | - | - |
| raw_search_tuned | 500 | 0.806 (403/500) | 0.793 | 0.728 (364/500) | 0.914 (457/500) | 0.931 (67/72) | 0.597 (43/72) | 0.014 (1/72) | 0.933 (28/30) | 0.109 (51/470) | +0.020 [-0.008, +0.048] | - |
| strata_no_nodes | 500 | 0.830 (415/500) | 0.811 | 0.766 (383/500) | 0.908 (454/500) | 0.931 (67/72) | 0.667 (48/72) | 0.042 (3/72) | 0.900 (27/30) | 0.079 (37/470) | +0.044 [+0.014, +0.076] | +0.024 [-0.002, +0.050] |
| strata_eager | 500 | 0.846 (423/500) | 0.826 | 0.774 (387/500) | 0.896 (448/500) | 0.931 (67/72) | 0.625 (45/72) | 0.069 (5/72) | 0.867 (26/30) | 0.070 (33/470) | +0.060 [+0.028, +0.096] | +0.040 [+0.010, +0.068] |
| strata_priority | 500 | 0.826 (413/500) | 0.813 | 0.756 (378/500) | 0.922 (461/500) | 0.903 (65/72) | 0.681 (49/72) | 0.042 (3/72) | 0.833 (25/30) | 0.077 (36/470) | +0.040 [+0.006, +0.074] | +0.020 [-0.010, +0.048] |
| strata_lazy | 500 | 0.854 (427/500) | 0.839 | 0.784 (392/500) | 0.910 (455/500) | 0.944 (68/72) | 0.681 (49/72) | 0.042 (3/72) | 0.900 (27/30) | 0.060 (28/470) | +0.068 [+0.036, +0.100] | +0.048 [+0.020, +0.076] |
| stale_control | 500 | 0.856 (428/500) | 0.849 | 0.798 (399/500) | 0.914 (457/500) | 0.944 (68/72) | 0.667 (48/72) | 0.042 (3/72) | 0.900 (27/30) | 0.068 (32/470) | +0.070 [+0.038, +0.104] | +0.050 [+0.022, +0.080] |
| digest_eager | 40 | 0.850 (34/40) | 0.828 | 0.775 (31/40) | 0.925 (37/40) | 1.000 (6/6) | 0.667 (4/6) | 0.000 (0/6) | 1.000 (2/2) | 0.053 (2/38) | +0.125 [+0.000, +0.250] | +0.050 [-0.100, +0.175] |
| digest_priority | 40 | 0.825 (33/40) | 0.801 | 0.725 (29/40) | 0.900 (36/40) | 0.833 (5/6) | 0.667 (4/6) | 0.167 (1/6) | 1.000 (2/2) | 0.079 (3/38) | +0.100 [-0.050, +0.225] | +0.025 [-0.075, +0.125] |
| digest_lazy | 40 | 0.800 (32/40) | 0.798 | 0.750 (30/40) | 0.950 (38/40) | 0.833 (5/6) | 0.667 (4/6) | 0.167 (1/6) | 1.000 (2/2) | 0.053 (2/38) | +0.075 [-0.075, +0.225] | +0.000 [-0.150, +0.125] |
| digest_stale | 40 | 0.825 (33/40) | 0.813 | 0.750 (30/40) | 0.925 (37/40) | 0.833 (5/6) | 0.833 (5/6) | 0.167 (1/6) | 1.000 (2/2) | 0.053 (2/38) | +0.100 [-0.025, +0.225] | +0.025 [-0.100, +0.150] |

| system | knowledge-update | multi-session | single-session-assistant | single-session-preference | single-session-user | temporal-reasoning |
|---|---|---|---|---|---|---|
| raw_search | 0.923 (72/78) | 0.571 (76/133) | 0.982 (55/56) | 0.600 (18/30) | 0.914 (64/70) | 0.812 (108/133) |
| raw_search_tuned | 0.923 (72/78) | 0.707 (94/133) | 0.929 (52/56) | 0.500 (15/30) | 0.886 (62/70) | 0.812 (108/133) |
| strata_no_nodes | 0.923 (72/78) | 0.707 (94/133) | 0.875 (49/56) | 0.533 (16/30) | 0.943 (66/70) | 0.887 (118/133) |
| strata_eager | 0.923 (72/78) | 0.759 (101/133) | 0.893 (50/56) | 0.567 (17/30) | 0.929 (65/70) | 0.887 (118/133) |
| strata_priority | 0.897 (70/78) | 0.722 (96/133) | 0.911 (51/56) | 0.567 (17/30) | 0.914 (64/70) | 0.865 (115/133) |
| strata_lazy | 0.936 (73/78) | 0.752 (100/133) | 0.911 (51/56) | 0.600 (18/30) | 0.943 (66/70) | 0.895 (119/133) |
| stale_control | 0.949 (74/78) | 0.759 (101/133) | 0.911 (51/56) | 0.667 (20/30) | 0.929 (65/70) | 0.880 (117/133) |
| digest_eager | 1.000 (6/6) | 0.727 (8/11) | 1.000 (4/4) | 0.500 (1/2) | 0.833 (5/6) | 0.909 (10/11) |
| digest_priority | 0.833 (5/6) | 0.727 (8/11) | 1.000 (4/4) | 0.500 (1/2) | 0.833 (5/6) | 0.909 (10/11) |
| digest_lazy | 0.833 (5/6) | 0.727 (8/11) | 1.000 (4/4) | 0.500 (1/2) | 1.000 (6/6) | 0.727 (8/11) |
| digest_stale | 0.833 (5/6) | 0.727 (8/11) | 1.000 (4/4) | 0.500 (1/2) | 1.000 (6/6) | 0.818 (9/11) |

| system | answer latency mean/p95 (s) | prompt tokens | completion tokens | memory-building tokens/q (extraction + digests) |
|---|---|---|---|---|
| raw_search | 8.32 / 17.64 | 4956 | 714.4 | 0 |
| raw_search_tuned | 8.76 / 17.62 | 5484 | 747.2 | 0 |
| strata_no_nodes | 8.75 / 18.26 | 5595 | 813.8 | 51009 |
| strata_eager | 9.10 / 19.21 | 5689 | 857.5 | 51009 |
| strata_priority | 8.97 / 20.16 | 5592 | 852.6 | 51009 |
| strata_lazy | 8.97 / 18.62 | 5617 | 861.2 | 51009 |
| stale_control | 9.26 / 20.25 | 5554 | 860.7 | 51009 |
| digest_eager | 9.45 / 16.95 | 5763 | 973.4 | 164409 |
| digest_priority | 7.69 / 15.75 | 5558 | 889.1 | 88372 |
| digest_lazy | 7.92 / 15.94 | 5603 | 928.4 | 57226 |
| digest_stale | 8.75 / 18.65 | 5800 | 975.5 | 92566 |

## Retrieval and maintenance (answer-model independent)

| system | retrieval ms mean/p50/p95 | fallback used | withheld nodes | dirty at query | stale items served | evidence in context | maint. jobs/q | maint. cost/q | wasted/q | lazy refreshed | ctx chars | topics/q | timelines/q | time-window questions |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raw_search | 0.7 / 0.5 / 1.6 | 1.000 (500/500) | 0.000 (0/500) | 0.000 (0/500) | 0 | 0.948 (454/479) | 0.0 | 0.0 | 0.0 | 0 | 21172 | 0.0 | 0.0 | 0 |
| raw_search_tuned | 0.9 / 0.8 / 1.7 | 1.000 (500/500) | 0.000 (0/500) | 0.000 (0/500) | 0 | 0.967 (463/479) | 0.0 | 0.0 | 0.0 | 0 | 23075 | 0.0 | 0.0 | 59 |
| strata_no_nodes | 1.2 / 1.1 / 2.2 | 0.004 (2/500) | 0.000 (0/500) | 0.000 (0/500) | 0 | 0.979 (469/479) | 0.0 | 0.0 | 0.0 | 0 | 23046 | 0.0 | 0.0 | 59 |
| strata_eager | 1.6 / 1.5 / 2.9 | 0.004 (2/500) | 0.000 (0/500) | 0.000 (0/500) | 0 | 0.977 (468/479) | 81.9 | 81.9 | 0.0 | 0 | 23091 | 8.5 | 11.2 | 59 |
| strata_priority | 1.7 / 1.4 / 3.1 | 0.734 (367/500) | 0.730 (365/500) | 0.976 (488/500) | 0 | 0.977 (468/479) | 44.2 | 44.2 | 0.0 | 0 | 23078 | 8.5 | 11.2 | 59 |
| strata_lazy | 4.0 / 3.5 / 8.2 | 0.032 (16/500) | 0.028 (14/500) | 1.000 (500/500) | 0 | 0.975 (467/479) | 0.0 | 0.0 | 0.0 | 1429 | 23052 | 8.5 | 11.2 | 59 |
| stale_control | 1.7 / 1.5 / 3.1 | 0.734 (367/500) | 0.730 (365/500) | 0.976 (488/500) | 828 | 0.975 (467/479) | 44.2 | 44.2 | 0.0 | 0 | 23089 | 8.5 | 11.2 | 59 |
| digest_eager | 4.4 / 3.8 / 8.3 | 0.000 (0/40) | 0.000 (0/40) | 0.000 (0/40) | 0 | 1.000 (38/38) | 153.1 | 111048.9 | 0.0 | 0 | 23381 | 8.7 | 12.3 | 4 |
| digest_priority | 1.9 / 1.6 / 3.7 | 0.925 (37/40) | 0.925 (37/40) | 1.000 (40/40) | 0 | 1.000 (38/38) | 115.7 | 35012.7 | 0.0 | 0 | 23171 | 8.7 | 12.3 | 4 |
| digest_lazy | 6.1 / 5.1 / 11.9 | 0.175 (7/40) | 0.175 (7/40) | 1.000 (40/40) | 0 | 1.000 (38/38) | 0.0 | 0.0 | 0.0 | 177 | 23235 | 8.7 | 12.3 | 4 |
| digest_stale | 2.4 / 2.1 / 5.3 | 0.925 (37/40) | 0.925 (37/40) | 1.000 (40/40) | 130 | 0.974 (37/38) | 118.3 | 39204.3 | 0.0 | 0 | 23485 | 8.7 | 12.3 | 4 |

## LLM-written digests (real token costs)

Tokens are the digest model's reported input + output tokens. The full-rebuild figure (eager row only) is an estimate: every digest re-written after every session, at its latest measured cost. Build-once is each digest built so far written once, at query time, at its latest measured cost.

| system | questions | digests | valid at query | model calls | tokens spent | full rebuild (est.) | build once at query (est.) |
|---|---|---|---|---|---|---|---|
| digest_eager | 40 | 348 | 348 | 2639 | 4438469 | 19251729 | 657859 |
| digest_priority | 40 | 348 | 150 | 1141 | 1397024 | - | 646839 |
| digest_lazy | 40 | 348 | 61 | 61 | 151151 | - | 151151 |
| digest_stale | 40 | 348 | 150 | 1343 | 1564781 | - | 660886 |
