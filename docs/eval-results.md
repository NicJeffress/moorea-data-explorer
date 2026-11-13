# Evaluation results

Built from 719 saved execution records in `evals/analyst-models/results/` by `scripts/build_showcase.py` in the private repository. First-answer extracts used by the judge are excluded because those executions already appear in the full result files. Each record has an ID derived from its experiment filename, row index and model; rescoring does not change that ID. Answers and tool arguments are not published.

## How to read these results

These are development experiments with different prompts, tool lists, question counts and scoring snapshots. The README pools their historical totals; it is not a matched model comparison or a measure of the current preview. Some question texts are omitted from this excerpt; numeric results retain the full question sets.

The inventory identifies the source commit and tool list when recorded. The scorer code revision was not recorded in these result files. Scores are exported as saved, without rescoring. The score hash identifies the exported score snapshot, not a scorer version. A missing tool-list label means not recorded; it must not be assumed to mean the eval list.

## Experiment inventory

| Experiment file | Model | Source commit | Earlier prompt commit | Mode | Tool list | Score snapshot (SHA-256 prefix) |
|---|---|---|---|---|---|---|
| [20261110-114221-claude-haiku-5-5-before-c0994ab.json](../evals/analyst-models/results/20261110-114221-claude-haiku-5-5-before-c0994ab.json) | claude-haiku-5-5@medium | c9cbdd0 | c0994ab | before | not recorded | 99edcccf8152 |
| [20261110-114641-claude-haiku-5-5-before-386d610.json](../evals/analyst-models/results/20261110-114641-claude-haiku-5-5-before-386d610.json) | claude-haiku-5-5@medium | c9cbdd0 | 386d610 | before | not recorded | 75adb8992c19 |
| [20261110-115006-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261110-115006-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | c9cbdd0 | — | after | not recorded | 900dcab0e092 |
| [20261110-115417-claude-haiku-5-5-before-386d610.json](../evals/analyst-models/results/20261110-115417-claude-haiku-5-5-before-386d610.json) | claude-haiku-5-5@medium | c9cbdd0 | 386d610 | before | not recorded | 2c610641b505 |
| [20261110-115441-claude-sonnet-5-5-before-386d610.json](../evals/analyst-models/results/20261110-115441-claude-sonnet-5-5-before-386d610.json) | claude-sonnet-5-5@medium | c9cbdd0 | 386d610 | before | not recorded | 09fe3d61c252 |
| [20261110-115640-claude-sonnet-5-5-after.json](../evals/analyst-models/results/20261110-115640-claude-sonnet-5-5-after.json) | claude-sonnet-5-5@medium | c9cbdd0 | — | after | not recorded | 599737aa0d09 |
| [20261110-115835-claude-opus-5-5-before-386d610.json](../evals/analyst-models/results/20261110-115835-claude-opus-5-5-before-386d610.json) | claude-opus-5-5@medium | c9cbdd0 | 386d610 | before | not recorded | 0a246966945f |
| [20261110-120617-claude-opus-5-5-after.json](../evals/analyst-models/results/20261110-120617-claude-opus-5-5-after.json) | claude-opus-5-5@medium | c9cbdd0 | — | after | not recorded | 9f56a9da3efe |
| [20261110-121106-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261110-121106-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | c9cbdd0 | — | after | not recorded | 4676d11fb0c0 |
| [20261110-124357-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-124357-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 96db7e8 | — | after | not recorded | 1edd96c70cb9 |
| [20261110-124755-claude-sonnet-5-5-questions.json](../evals/analyst-models/results/20261110-124755-claude-sonnet-5-5-questions.json) | claude-sonnet-5-5@medium | 96db7e8 | — | after | not recorded | 0f174e5cdaaf |
| [20261110-134715-claude-opus-5-5-questions.json](../evals/analyst-models/results/20261110-134715-claude-opus-5-5-questions.json) | claude-opus-5-5@medium | c971780 | — | after | not recorded | 83c7023f653a |
| [20261110-134840-claude-haiku-5-5-before-table-14daa19.json](../evals/analyst-models/results/20261110-134840-claude-haiku-5-5-before-table-14daa19.json) | claude-haiku-5-5@medium | a21a25d | 14daa19 | before-table | not recorded | 4c69d3011335 |
| [20261110-135130-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-135130-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | c971780 | — | after | not recorded | 81eccd6aaaba |
| [20261110-135130-claude-opus-5-5-questions.json](../evals/analyst-models/results/20261110-135130-claude-opus-5-5-questions.json) | claude-opus-5-5@medium | c971780 | — | after | not recorded | 8ce420f83f99 |
| [20261110-135207-claude-sonnet-5-5-questions.json](../evals/analyst-models/results/20261110-135207-claude-sonnet-5-5-questions.json) | claude-sonnet-5-5@medium | c971780 | — | after | not recorded | 60778510a0f5 |
| [20261110-135226-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261110-135226-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | a21a25d | — | after | not recorded | 297b8c339339 |
| [20261110-135828-claude-opus-5-5-questions.json](../evals/analyst-models/results/20261110-135828-claude-opus-5-5-questions.json) | claude-opus-5-5@medium | c971780 | — | after | not recorded | a4ef4544d2d2 |
| [20261110-144012-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-144012-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 9ffae0f | — | after | not recorded | a990b8f8e9fc |
| [20261110-145156-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-145156-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 9ffae0f | — | after | not recorded | 5974db359157 |
| [20261110-153041-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-153041-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 8dcfc91 | — | after | not recorded | c83145d06d8d |
| [20261110-231219-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-231219-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 367d697 | — | after | not recorded | c8eaec26d45d |
| [20261110-231509-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261110-231509-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 367d697 | — | after | not recorded | dd3cb52c540c |
| [20261111-142628-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261111-142628-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | not recorded | ae9d037e58b4 |
| [20261111-142945-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261111-142945-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | not recorded | 9277e42eedcc |
| [20261111-143150-claude-haiku-5-5-questions-live.json](../evals/analyst-models/results/20261111-143150-claude-haiku-5-5-questions-live.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | live | 47439e5f5f63 |
| [20261111-143150-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261111-143150-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | eval | fa29d989559b |
| [20261111-143240-claude-haiku-5-5-after-live.json](../evals/analyst-models/results/20261111-143240-claude-haiku-5-5-after-live.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | live | 51e62f069b2c |
| [20261111-143240-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261111-143240-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | eval | 024a23e030ba |
| [20261111-143602-claude-haiku-5-5-main-w6-w8.json](../evals/analyst-models/results/20261111-143602-claude-haiku-5-5-main-w6-w8.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | not recorded | 01aa3f61077e |
| [20261111-143917-claude-haiku-5-5-after.json](../evals/analyst-models/results/20261111-143917-claude-haiku-5-5-after.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | not recorded | 046b49bf780e |
| [20261111-144355-claude-haiku-5-5-questions.json](../evals/analyst-models/results/20261111-144355-claude-haiku-5-5-questions.json) | claude-haiku-5-5@medium | 142c5c6 | — | after | not recorded | 6e8cf3a3763d |
| [20261112-144641-claude-haiku-5-5-after-live.json](../evals/analyst-models/results/20261112-144641-claude-haiku-5-5-after-live.json) | claude-haiku-5-5@medium | e69b10b | — | after | live | 200fdad5bf37 |

## Exact checks by experiment

A run passes when every saved automatic check passes. The denominator is the number of runs with an exact verdict, shown separately from total saved runs.

| Experiment file | Group | Saved runs | Passed / graded | Mean cost per saved run | Seconds per saved run |
|---|---|---:|---:|---:|---:|
| 20261110-124357-claude-haiku-5-5-questions.json | Data questions with one right answer | 10 | 7/10 | $0.0055 | 13 |
| 20261110-124357-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 6 | 4/6 | $0.0100 | 25 |
| 20261110-124357-claude-haiku-5-5-questions.json | The same questions in French | 3 | 2/3 | $0.0083 | 23 |
| 20261110-124755-claude-sonnet-5-5-questions.json | Data questions with one right answer | 10 | 10/10 | $0.0690 | 10 |
| 20261110-124755-claude-sonnet-5-5-questions.json | "Show me" requests (opens the right page) | 6 | 4/6 | $0.1114 | 24 |
| 20261110-124755-claude-sonnet-5-5-questions.json | The same questions in French | 3 | 3/3 | $0.0943 | 19 |
| 20261110-134715-claude-opus-5-5-questions.json | Data questions with one right answer | 10 | 9/10 | $0.1825 | 24 |
| 20261110-134715-claude-opus-5-5-questions.json | "Show me" requests (opens the right page) | 6 | 5/6 | $0.3158 | 47 |
| 20261110-134715-claude-opus-5-5-questions.json | The same questions in French | 3 | 3/3 | $0.2398 | 37 |
| 20261110-135130-claude-haiku-5-5-questions.json | Data questions with one right answer | 20 | 18/20 | $0.0047 | 13 |
| 20261110-135130-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 12 | 8/12 | $0.0091 | 26 |
| 20261110-135130-claude-haiku-5-5-questions.json | The same questions in French | 6 | 6/6 | $0.0071 | 21 |
| 20261110-135207-claude-sonnet-5-5-questions.json | Data questions with one right answer | 20 | 19/20 | $0.0463 | 9 |
| 20261110-135207-claude-sonnet-5-5-questions.json | "Show me" requests (opens the right page) | 12 | 10/12 | $0.0820 | 18 |
| 20261110-135207-claude-sonnet-5-5-questions.json | The same questions in French | 6 | 6/6 | $0.0712 | 16 |
| 20261110-135828-claude-opus-5-5-questions.json | Data questions with one right answer | 20 | 20/20 | $0.1477 | 24 |
| 20261110-135828-claude-opus-5-5-questions.json | "Show me" requests (opens the right page) | 12 | 12/12 | $0.2807 | 46 |
| 20261110-135828-claude-opus-5-5-questions.json | The same questions in French | 6 | 6/6 | $0.2223 | 42 |
| 20261110-144012-claude-haiku-5-5-questions.json | Data questions with one right answer | 30 | 27/30 | $0.0043 | 13 |
| 20261110-144012-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 18 | 18/18 | $0.0068 | 19 |
| 20261110-144012-claude-haiku-5-5-questions.json | The same questions in French | 9 | 9/9 | $0.0062 | 22 |
| 20261110-153041-claude-haiku-5-5-questions.json | Data questions with one right answer | 3 | 1/3 | $0.0088 | 20 |
| 20261110-231219-claude-haiku-5-5-questions.json | Data questions with one right answer | 3 | 2/3 | $0.0066 | 19 |
| 20261110-231509-claude-haiku-5-5-questions.json | Data questions with one right answer | 3 | 3/3 | $0.0037 | 14 |
| 20261111-142945-claude-haiku-5-5-questions.json | Data questions with one right answer | 30 | 28/30 | $0.0040 | 11 |
| 20261111-142945-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 18 | 18/18 | $0.0060 | 17 |
| 20261111-142945-claude-haiku-5-5-questions.json | The same questions in French | 9 | 9/9 | $0.0067 | 20 |
| 20261111-143150-claude-haiku-5-5-questions-live.json | Data questions with one right answer | 30 | 28/30 | $0.0044 | 10 |
| 20261111-143150-claude-haiku-5-5-questions-live.json | "Show me" requests (opens the right page) | 18 | 18/18 | $0.0071 | 18 |
| 20261111-143150-claude-haiku-5-5-questions-live.json | The same questions in French | 9 | 9/9 | $0.0064 | 16 |
| 20261111-143150-claude-haiku-5-5-questions.json | Data questions with one right answer | 30 | 30/30 | $0.0028 | 11 |
| 20261111-143150-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 18 | 18/18 | $0.0054 | 17 |
| 20261111-143150-claude-haiku-5-5-questions.json | The same questions in French | 9 | 9/9 | $0.0056 | 19 |
| 20261111-144355-claude-haiku-5-5-questions.json | Data questions with one right answer | 30 | 29/30 | $0.0039 | 11 |
| 20261111-144355-claude-haiku-5-5-questions.json | "Show me" requests (opens the right page) | 18 | 18/18 | $0.0063 | 18 |
| 20261111-144355-claude-haiku-5-5-questions.json | The same questions in French | 9 | 9/9 | $0.0055 | 18 |

## Cost by experiment, all question groups

Includes groups graded by a second model; that judge's own cost is not included here.

| Experiment file | Saved runs | Mean cost per saved run | Total |
|---|---:|---:|---:|
| 20261110-124357-claude-haiku-5-5-questions.json | 33 | $0.0070 | $0.23 |
| 20261110-124755-claude-sonnet-5-5-questions.json | 33 | $0.0801 | $2.64 |
| 20261110-134715-claude-opus-5-5-questions.json | 19 | $0.2337 | $4.44 |
| 20261110-135130-claude-haiku-5-5-questions.json | 66 | $0.0061 | $0.40 |
| 20261110-135130-claude-opus-5-5-questions.json | 14 | $0.2340 | $3.28 |
| 20261110-135207-claude-sonnet-5-5-questions.json | 38 | $0.0615 | $2.34 |
| 20261110-135828-claude-opus-5-5-questions.json | 38 | $0.2015 | $7.66 |
| 20261110-144012-claude-haiku-5-5-questions.json | 99 | $0.0049 | $0.48 |
| 20261110-145156-claude-haiku-5-5-questions.json | 6 | $0.0022 | $0.01 |
| 20261110-153041-claude-haiku-5-5-questions.json | 6 | $0.0088 | $0.05 |
| 20261110-231219-claude-haiku-5-5-questions.json | 6 | $0.0074 | $0.04 |
| 20261110-231509-claude-haiku-5-5-questions.json | 3 | $0.0037 | $0.01 |
| 20261111-142945-claude-haiku-5-5-questions.json | 57 | $0.0051 | $0.29 |
| 20261111-143150-claude-haiku-5-5-questions-live.json | 57 | $0.0056 | $0.32 |
| 20261111-143150-claude-haiku-5-5-questions.json | 57 | $0.0040 | $0.23 |
| 20261111-144355-claude-haiku-5-5-questions.json | 57 | $0.0049 | $0.28 |

## Sampling checks by experiment

The inventory records which earlier commit a before run replayed. Each answer has several automatic checks; this table counts checks passed, not fully passing answers.

| Experiment file | Question | Saved runs | Checks passed | Mean cost per saved run |
|---|---|---:|---:|---:|
| 20261110-114221-claude-haiku-5-5-before-c0994ab.json | W1 | 3 | 10/15 (67%) | $0.0099 |
| 20261110-114221-claude-haiku-5-5-before-c0994ab.json | W2 | 3 | 10/12 (83%) | $0.0045 |
| 20261110-114221-claude-haiku-5-5-before-c0994ab.json | W3 | 3 | 12/12 (100%) | $0.0075 |
| 20261110-114641-claude-haiku-5-5-before-386d610.json | W1 | 3 | 12/15 (80%) | $0.0082 |
| 20261110-114641-claude-haiku-5-5-before-386d610.json | W2 | 3 | 12/12 (100%) | $0.0044 |
| 20261110-114641-claude-haiku-5-5-before-386d610.json | W3 | 3 | 8/8 (100%) | $0.0032 |
| 20261110-115006-claude-haiku-5-5-after.json | W1 | 3 | 14/15 (93%) | $0.0077 |
| 20261110-115006-claude-haiku-5-5-after.json | W2 | 3 | 9/12 (75%) | $0.0053 |
| 20261110-115006-claude-haiku-5-5-after.json | W3 | 3 | 9/12 (75%) | $0.0059 |
| 20261110-115417-claude-haiku-5-5-before-386d610.json | W3 | 1 | 4/4 (100%) | $0.0043 |
| 20261110-115441-claude-sonnet-5-5-before-386d610.json | W1 | 2 | 8/10 (80%) | $0.0910 |
| 20261110-115441-claude-sonnet-5-5-before-386d610.json | W2 | 2 | 7/8 (88%) | $0.0511 |
| 20261110-115441-claude-sonnet-5-5-before-386d610.json | W3 | 2 | 5/8 (62%) | $0.0532 |
| 20261110-115640-claude-sonnet-5-5-after.json | W1 | 2 | 10/10 (100%) | $0.0693 |
| 20261110-115640-claude-sonnet-5-5-after.json | W2 | 2 | 8/8 (100%) | $0.0549 |
| 20261110-115640-claude-sonnet-5-5-after.json | W3 | 2 | 6/8 (75%) | $0.0533 |
| 20261110-115835-claude-opus-5-5-before-386d610.json | W1 | 2 | 8/10 (80%) | $0.5050 |
| 20261110-115835-claude-opus-5-5-before-386d610.json | W2 | 2 | 7/8 (88%) | $0.3342 |
| 20261110-115835-claude-opus-5-5-before-386d610.json | W3 | 2 | 8/8 (100%) | $0.4316 |
| 20261110-120617-claude-opus-5-5-after.json | W1 | 2 | 9/10 (90%) | $0.2607 |
| 20261110-120617-claude-opus-5-5-after.json | W2 | 2 | 6/8 (75%) | $0.2122 |
| 20261110-120617-claude-opus-5-5-after.json | W3 | 2 | 8/8 (100%) | $0.1773 |
| 20261110-121106-claude-haiku-5-5-after.json | W1 | 3 | 15/15 (100%) | $0.0070 |
| 20261110-121106-claude-haiku-5-5-after.json | W2 | 3 | 11/12 (92%) | $0.0054 |
| 20261110-121106-claude-haiku-5-5-after.json | W3 | 3 | 10/12 (83%) | $0.0056 |
| 20261110-134840-claude-haiku-5-5-before-table-14daa19.json | W4 | 3 | 13/15 (87%) | $0.0102 |
| 20261110-134840-claude-haiku-5-5-before-table-14daa19.json | W5 | 3 | 13/15 (87%) | $0.0106 |
| 20261110-135226-claude-haiku-5-5-after.json | W4 | 3 | 14/15 (93%) | $0.0122 |
| 20261110-135226-claude-haiku-5-5-after.json | W5 | 3 | 15/15 (100%) | $0.0115 |
| 20261111-142628-claude-haiku-5-5-after.json | W1 | 3 | 15/15 (100%) | $0.0066 |
| 20261111-142628-claude-haiku-5-5-after.json | W2 | 3 | 12/12 (100%) | $0.0062 |
| 20261111-142628-claude-haiku-5-5-after.json | W3 | 3 | 11/12 (92%) | $0.0051 |
| 20261111-143240-claude-haiku-5-5-after-live.json | W1 | 3 | 15/15 (100%) | $0.0076 |
| 20261111-143240-claude-haiku-5-5-after-live.json | W2 | 3 | 9/12 (75%) | $0.0059 |
| 20261111-143240-claude-haiku-5-5-after-live.json | W3 | 3 | 12/12 (100%) | $0.0053 |
| 20261111-143240-claude-haiku-5-5-after.json | W1 | 3 | 15/15 (100%) | $0.0063 |
| 20261111-143240-claude-haiku-5-5-after.json | W2 | 3 | 8/12 (67%) | $0.0044 |
| 20261111-143240-claude-haiku-5-5-after.json | W3 | 3 | 11/12 (92%) | $0.0053 |
| 20261111-143602-claude-haiku-5-5-main-w6-w8.json | W6 | 3 | 12/12 (100%) | $0.0038 |
| 20261111-143602-claude-haiku-5-5-main-w6-w8.json | W7 | 3 | 12/12 (100%) | $0.0059 |
| 20261111-143602-claude-haiku-5-5-main-w6-w8.json | W8 | 3 | 13/15 (87%) | $0.0035 |
| 20261111-143917-claude-haiku-5-5-after.json | W1 | 3 | 15/15 (100%) | $0.0040 |
| 20261111-143917-claude-haiku-5-5-after.json | W2 | 3 | 12/12 (100%) | $0.0041 |
| 20261111-143917-claude-haiku-5-5-after.json | W3 | 3 | 12/12 (100%) | $0.0035 |
| 20261111-143917-claude-haiku-5-5-after.json | W6 | 3 | 12/12 (100%) | $0.0040 |
| 20261111-143917-claude-haiku-5-5-after.json | W7 | 3 | 12/12 (100%) | $0.0045 |
| 20261111-143917-claude-haiku-5-5-after.json | W8 | 3 | 15/15 (100%) | $0.0026 |
| 20261112-144641-claude-haiku-5-5-after-live.json | M1 | 3 | 24/30 (80%) | $0.0121 |
