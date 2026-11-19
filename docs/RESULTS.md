# BudgetPilot paired A/B results

Experiment `ab_20261115_160545`. Written by `bp ab`; do not edit by hand. Raw data: `docs/results/ab_20261115_160545.json` (when archived) or `bench_results/`.

## Result

**Saving: the whole 95% interval is below zero and the pass rate did not drop.**

- Billing units, enforce compared with observe: **-15.5%** per pair on average (95% bootstrap CI -20.4% to -9.8%, 6 pairs).
- In units: -9,947 per run (95% CI -14,496 to -5,090).
- Tasks passed: observe 6/6, enforce 6/6.

A negative change means BudgetPilot used fewer billing units. A saving only counts if the pass rate does not drop.

## Setup

| Item | Value |
|---|---|
| Host | 2.1.325 (Claude Code), headless (`claude -p`), `--permission-mode acceptEdits` |
| Model | `haiku` in both arms, default effort, a fresh session per run |
| Arms | `observe` (hooks measure, never intervene) and `enforce` (hooks act), `BP_HITL=0` |
| Runs | 12 = 3 tasks x 2 repeats x 2 arms, in random order (seed 0) |
| Repository | `bench/fixture_repo` (synthetic, about 3,300 lines; one 3,155-line file) |
| Success | a hidden test per task, copied in after the agent stops |
| Allowed tools | `Read,Edit,Write,Grep,Glob,Bash(pytest *),Bash(python -m pytest *)` |
| Billing | not recorded (this run predates billing detection); `bp ab` removes ANTHROPIC_API_KEY, so the test sessions use the plan login |
| Cost measure | billing units (relative weighting, not dollars) from the session transcript: input + 1.25 x 5-minute cache writes + 2 x 1-hour cache writes + r x cache reads + 5 x output |

## Per arm

| Arm | Passed | Mean billing units | Mean turns | Mean output tokens | Cache-hit ratio | BudgetPilot text added (est. tokens) | Mean wall time (s) |
|---|---|---|---|---|---|---|---|
| enforce | 6/6 | 49,352 | 4.3 | 1,566 | 0.846 | 47 | 13 |
| observe | 6/6 | 59,299 | 5.7 | 2,259 | 0.873 | 0 | 16 |

## Per task

| Task | Scope | Held out | Pairs | Change in billing units | Passed (observe) | Passed (enforce) |
|---|---|---|---|---|---|---|
| t1_discount | 1 | no | 2 | -12.8% | 2/2 | 2/2 |
| t4_eu_tax | 2 | no | 2 | -11.8% | 2/2 | 2/2 |
| t6_rename_format_money | 3 | no | 2 | -22.0% | 2/2 | 2/2 |

## Where the difference came from

- Model turns per run: 4.3 with BudgetPilot on, 5.7 observing.
- Output tokens per run: 1,566 on, 2,259 observing.
- Calls the gate changed in the enforce arm: 4 rewritten, 0 denied or sent to a dialog.
- Text BudgetPilot added per run: about 47 tokens (estimated).

The gate changed few calls, so in this run the difference did **not** come from blocked or narrowed tool calls. It came from the short notes BudgetPilot adds at the start (the scope limits and the terse-output contract): the model made fewer tool calls and wrote less. The observe-arm table below shows calls the baseline agent made that the enforce-arm agent did not attempt. Fewer verification runs can cost correctness on harder tasks than these; the pass rate above is the check.

### Gate decisions in the enforce arm (all runs)

| Decision : rule | Count |
|---|---|
| `rewrite:grep_head_limit` | 4 |

### What the gate would have done in the observe arm (logged, not applied)

| Decision : rule | Count |
|---|---|
| `would_rewrite:grep_head_limit` | 8 |
| `would_deny:bash_grep_recursive` | 2 |
| `would_deny:bash_full_suite` | 2 |
| `would_deny:bash_ls_recursive` | 1 |

## Checks

- Transcript totals compared with the `usage` block of `claude -p --output-format json`: largest difference 0 tokens in any field of any run.
- Runs in which a hook logged an internal error: 0.
- Pairs where both arms had the same pass/fail status: 6; mean saving on those 9,947 units (95% CI 5,090 to 14,496).

- Price check: BudgetPilot's API-equivalent cost against Claude Code's own `total_cost_usd`: largest difference 0.01% over 12 runs (within the 2% tolerance).

## Caveats

- Small sample: 6 pairs. Treat the interval, not the point estimate, as the result.
- One synthetic repository and one model. Results on real repositories and other models can differ.
- Headless runs: the human-in-the-loop recovery path is not exercised (`BP_HITL=0`).
- Billing units are API-equivalent weights. A Pro or Max plan is not billed per token, and the plan's own usage weights are not published. The test sessions are not billed per token, so no dollar figures are shown.
- The observe arm still runs the hook processes, so it measures the effect of BudgetPilot's interventions, not of installing it. Latency added by the hooks is not part of billing units.
- Held-out tasks (none in this run) must not be used to tune scope limits.

## Scope limits used

```json
{
 "1": {
  "allow_full_suite_tests": false,
  "allow_repo_wide_search": false,
  "context_budget_tokens": 6000,
  "edit_max_new_lines": 60,
  "grep_head_limit": 50,
  "list_depth": 1,
  "max_files_outside_named": 3,
  "output_budget_tokens": 1500,
  "read_window_lines": 400
 },
 "2": {
  "allow_full_suite_tests": false,
  "allow_repo_wide_search": false,
  "context_budget_tokens": 16000,
  "edit_max_new_lines": 150,
  "grep_head_limit": 120,
  "list_depth": 2,
  "max_files_outside_named": 8,
  "output_budget_tokens": 4000,
  "read_window_lines": 800
 },
 "3": {
  "allow_full_suite_tests": true,
  "allow_repo_wide_search": true,
  "context_budget_tokens": 40000,
  "edit_max_new_lines": 400,
  "grep_head_limit": 250,
  "list_depth": null,
  "max_files_outside_named": 25,
  "output_budget_tokens": 10000,
  "read_window_lines": null
 }
}
```

## Reproduce

```bash
bp ab smoke                 # 3 tasks x 2 arms x 2 repeats
bp ab run --repeats 3       # all tasks
```
