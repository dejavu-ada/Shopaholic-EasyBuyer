# EasyBuyer Evaluation

This folder contains the saved runtime evidence and the completed Level 2 evaluation assignments for the 30-query project evaluation.

## Files

- `evaluation_runs.jsonl` stores the 30 app runs, including each query, parsed intent, returned products, data source, response time, and estimated OpenRouter cost. New runs are appended when evaluation logging is enabled.
- `tester_assignments.csv` is the evaluation record. It assigns five cases to each of six testers. It includes the query and recommendation list, tester ID and major, Accept/Reject decision, rejection reason, recommendation relevance counts, budget checks, valid-link counts, ranking score, response time, and estimated API cost.

The completed record contains 28 accepted and 2 rejected queries (28/30 = 93.3%). Rejection reasons are recorded for the umbrella result and the running-shoes product links. Budget fields are `N/A` for queries without a stated budget. Ranking scores use a 1–5 scale, where 1 indicates poor ordering and 5 indicates the best-matching products appear first.

## Runtime logging

Enable append-only logging before starting the app:

```bash
export EVALUATION_LOG_ENABLED=1
python shopaholic_easybuyer.py
```

Each successful or failed request is appended to `evals/evaluation_runs.jsonl`. The log may contain user query text. Disable logging with `EVALUATION_LOG_ENABLED=0` or unset the variable when it is no longer needed.
