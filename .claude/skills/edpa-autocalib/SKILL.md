---
name: edpa-autocalib
user-invocable: false
description: >
  Auto-calibrate EDPA CW heuristics using Karpathy's autoresearch loop. One file
  (cw_heuristics.yaml), one metric (MAD vs ground truth), one loop (modify→evaluate→keep/discard).
  Use when: user says "calibrate CW", "auto-calibrate", "optimize heuristics", or after first PI
  with ≥20 manually confirmed CW records. Do NOT use before first PI — insufficient ground truth.
license: MIT
compatibility: Python 3.10+, git, .edpa/config/heuristics.yaml, .edpa/data/ground_truth.yaml
allowed-tools: Read Write Bash(python3 *) Bash(git *) Grep
disable-model-invocation: false
metadata:
  author: Jaroslav Urbánek
  version: 1.0.0
  domain: governance
  phase: auto-calibration
  pattern: autoresearch (Karpathy)
  standard: AgentSkills v1.0
---

# EDPA Auto-Calibration — Karpathy Autoresearch Loop

## What this does

Optimizes CW heuristic parameters by running an autonomous experiment loop against
manually confirmed ground truth data. Inspired by Karpathy's autoresearch pattern.

## Arguments

`$ARGUMENTS` = experiment budget (number, default: 50) or "auto".

### Argument resolution (when $ARGUMENTS is empty)

If `$ARGUMENTS` is empty, blank, or "help":

1. Check if `.edpa/data/ground_truth.yaml` exists:
   - If yes, count records and report: "Ground truth: **{count}** records (minimum 20 required)."
   - If no, inform: "No ground truth file found. Create `.edpa/data/ground_truth.yaml` from retrospective data first."
2. If sufficient records, check current heuristics state:
   - Read `.edpa/config/heuristics.yaml` and show current role_weights
   - Check if `.edpa/data/calibration_log.tsv` exists → show last calibration date and MAD
3. Present:
   ```
   Calibration status:
     Ground truth:    32 records (OK)
     Current MAD:     0.12 (from last calibration 2026-03-15)
     Suggested budget: 50 experiments

   Run calibration with 50 experiments? [50]
   ```
4. **Default suggestion:** 50 experiments. User can override with any number.

## Prerequisites

**Hard requirement:** `.edpa/data/ground_truth.yaml` with ≥ 20 records:
```yaml
# .edpa/data/ground_truth.yaml — from retrospective confirmations
records:
  - item_id: S-200
    person_id: turyna
    evidence_role: owner
    auto_cw: 1.0
    confirmed_cw: 0.95
    iteration: PI-2026-1.1
```

If fewer than 20 records exist, inform user: "Insufficient ground truth. Need ≥20 confirmed CW records from retrospectives. Current: {count}."

## Configuration

```
Target file:    .edpa/config/heuristics.yaml
Metric:         mean_absolute_deviation(auto_cw, confirmed_cw)
Direction:      lower
Budget:         $0 (default 50) or user-specified experiments
Eval script:    .claude/edpa/scripts/evaluate_cw.py (LOCKED — agent must NOT edit)
Branch:         calibration/{timestamp}
```

## Autoresearch loop

### Setup
```bash
git checkout -b calibration/$(date +%Y%m%d-%H%M%S)
```

### Loop (repeat $0 times, default 50):

**Step 1: Read state**
- Load current `.edpa/config/heuristics.yaml`
- Load experiment history from git log on calibration branch
- Load last MAD score

**Step 2: Propose ONE change**
Pick one parameter to mutate:
- `role_weights.owner` ± small delta
- `role_weights.key` ± small delta
- `role_weights.reviewer` ± small delta
- `role_weights.consulted` ± small delta
- `signals.*` ± small delta
- `evidence_threshold` ± small delta

Constraints:
- owner ≥ key ≥ reviewer ≥ consulted (ordering preserved)
- All weights in [0.05, 1.0]
- Threshold in [0.5, 3.0]

**Step 3: Commit**
```bash
git add .edpa/config/heuristics.yaml
git commit -m "experiment {n}: {parameter} {old_value} -> {new_value}"
```

**Step 4: Evaluate**
```bash
python3 .claude/edpa/scripts/evaluate_cw.py --ground-truth .edpa/data/ground_truth.yaml --heuristics .edpa/config/heuristics.yaml
```

Script outputs: `MAD={value}`

**CRITICAL: Agent must NEVER edit .claude/edpa/scripts/evaluate_cw.py. If tempted to modify the evaluator, STOP. This separation prevents gaming.**

**Step 5: Decide**
- If new MAD < previous best MAD → **KEEP** (advance branch)
- If new MAD ≥ previous best MAD → **REVERT**: `git reset --hard HEAD~1`

**Step 6: Log**
Append to `.edpa/data/calibration_log.tsv`:
```
{experiment_num}\t{parameter}\t{old_value}\t{new_value}\t{new_MAD}\t{best_MAD}\t{kept|reverted}
```

### Post-loop

1. Print summary: experiments run, kept count, initial MAD, final MAD, % improvement
2. Print optimized parameters vs initial
3. Ask user: "Apply calibrated heuristics to main branch? (git merge calibration/... into main)"

## Strategy escalation

- Experiments 1–10: role_weights only (biggest impact)
- Experiments 11–25: signal weights
- Experiments 26–50: threshold + combined adjustments
- Every 10 experiments: review log for patterns, adjust mutation delta size

## Error handling

- Ground truth < 20 records → refuse, explain why
- evaluate_cw.py missing → create from template in .claude/edpa/scripts/evaluate_cw.py
- Git conflicts → abort, report
- MAD not improving after 20 experiments → suggest reviewing ground truth quality
