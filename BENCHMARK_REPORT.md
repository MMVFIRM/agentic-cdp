# Agentic CDP v0.1 — recall iteration benchmark report

**Run date:** 2026-09-29  
**Resolver:** r15, deterministic adjudicator  
**Environment:** Windows, Python 3.12, SQLite; synthetic customer data; vendor HTTP mocks  
**Gate policy:** registered `bench/gates.json` thresholds unchanged. The enterprise precision target is 0.995.

## Results

All four fixed production workloads and the fresh seed-84 holdout clear both the 0.995 precision target and the 0.90 recall gate. All 17 emitted safety checks (G1–G13, with G11 subchecks) pass on those five runs. The separate 45-person smoke passes the identity metrics and the other checks, but fails G12 at 6.667% review load.

| Workload | Records | Pair precision | Pair recall | Change vs r12 | F1 | Review load | Runtime | Gates |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1,500 people, seed 7 | 3,699 | 0.99884 | 0.92988 | +0.19 pp recall | 0.96313 | 3.731% | 41.8 s | All pass |
| 1,500 people, seed 21 | 3,693 | 0.99970 | 0.91619 | +0.49 pp recall | 0.95613 | 3.276% | 41.6 s | All pass |
| 1,500 people, seed 42 | 3,685 | 0.99518 | 0.90744 | +0.71 pp recall | 0.94929 | 4.478% | 41.9 s | All pass |
| 5,000 people, seed 7 | 12,219 | 0.99856 | 0.91533 | +0.65 pp recall | 0.95513 | 4.387% | 146.0 s | All pass |
| 1,500 people, seed 84 (fresh holdout) | 3,666 | 0.99540 | 0.91650 | +0.48 pp recall vs paired r12 baseline | 0.95433 | 4.419% | 42.1 s | All pass |
| 45 people, seed 7 (small smoke) | 105 | 1.00000 | 0.92308 | 0.00 pp recall vs r12 | 0.96000 | 6.667% | 2.2 s | G12 fails |

On the fixed production matrix, mean recall gain is 0.51 percentage points versus r12. True-positive pair counts increased by 7, 18, 26, and 79 on the four separate runs. On the paired seed-84 holdout, true-positive pairs increased from 3,232 to 3,249 (+17); precision improved from 0.99172 to 0.99540 and recall from 0.91171 to 0.91650.

The tightest precision score is 0.99518 on seed 42, just 0.018 percentage points above the requested floor. This is a benchmark pass with limited headroom, not evidence that the same margin will hold under customer data shift. The 45-person smoke remains a real G12 failure: seven queued reviews over 105 source records. Its coarse denominator makes each review move the rate by about one percentage point, but the registered threshold was left unchanged.

G8 consent violation rates were 0.164% (seed 7), 0% (seed 21), 0% (seed 42), 0.062% (5,000 people), and 0.167% (holdout); no pure denied profile was exported. G3 distinguishable hard-negative merges remained zero. Indistinguishable Jr/Sr clusters remain possible when source records omit suffix/DOB and share household contact data.

## What changed in r13–r15

- Added a deterministic exact/near-name path only when a shared contact is corroborated by exact locality. A4 requires citations for the name, shared contact, and locality evidence.
- Marked a suffix present on only one record as `partial`; such pairs cannot use the new shared-contact path.
- Added a constrained-cluster guard: in an exact-name, same-postal cohort already flagged ambiguous, a shared phone alone does not join profiles. Shared DOB or unambiguous email can still corroborate identity.
- Bumped the resolver version to `2026.09-r15` so cached pair decisions are recomputed.

The full test suite passes (**46 passed**, with one upstream Starlette/httpx deprecation warning). A missed-pair review found many residual cases lack a shared strong identifier or have suffix/locality ambiguity. Broader name-only or locality-only matching would put the precision floor at risk, so ambiguous cases remain separate or reviewable.

## Scope and limits

These are synthetic benchmark results, not a guarantee of the same performance on customer data. Connectors were exercised against documented-shape HTTP mocks, not live vendor tenants. No external LLM was called. These runs do not establish legal compliance, concurrency behavior, or throughput beyond the tested 12,219 source records in this local environment. Seed 84 is one fresh holdout, so more independent customer-like data is needed to characterize generalization. Precision headroom on seed 42 is only 0.018 percentage points, and the small smoke case fails G12 as reported above.

## Reproduction

From `work/agentic-cdp`:

- Tests: `.venv\Scripts\python.exe -m pytest -q`
- Fixed runs: `.venv\Scripts\python.exe -m bench.run --people <count> --seed <seed> --out <new-output-directory>`
- Fixed r15 JSON and gate reports: `iter20/n1500-seed7/`, `iter20/n1500-seed21/`, `iter20/n1500-seed42/`, and `iter20/n5000-seed7/`.
- Holdout and smoke reports: `iter20/n1500-seed84/` and `iter20/n45-seed7/`.

The unchanged thresholds, safety-gate details, earlier experiments, and method history are in `work/agentic-cdp/GATES.md`.
