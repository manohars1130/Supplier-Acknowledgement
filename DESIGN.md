# Design notes: problem, assumptions, decisions, testing, limitations

## 1. Problem as understood
Buyers issue POs; suppliers answer with acknowledgements that may accept, partially accept, reject, re-date or re-price lines, may revise later, or stay silent.
Each deviation must be compared with what was requested, routed through approval thresholds, applied to projected supply **only after approval**, and the affected planners told.
The deliverable is a *dataset + tooling* in which outcomes and labels come from documented rules (not random draws), history is never overwritten, four named edge cases are injected **and labelled separately**, and everything is reproducible from a seed.

## 2. Key assumptions (where the brief was open)
| # | Assumption | Why |
|---|---|---|
| A1 | `CHG-<PO-id>-000001` counter is **per PO**; NOT/EVT/IPL counters are global in time order | Brief shows the counter inside a PO-scoped ID |
| A2 | Acknowledgement = header + `acknowledgement_line` | Accepted/rejected qty and promise date are per line; added table, documented |
| A3 | A "change" = difference between an ack revision and the **current approved plan**; thresholds are measured on deviation from the **original request** | Satisfies "compare changes with requested values" and stops creeping slips hiding under thresholds |
| A4 | Single currency (USD), no goods-receipt events, as-of snapshot 2026-10-22 | Keeps thresholds/arithmetic unambiguous; receipt is out of scope |
| A5 | "15,000 acknowledgement/change events" = acknowledgements + change requests, hit **exactly** by a revision loop | Makes the volume verifiable |
| A6 | Ground-truth label for a change = the rule-engine's expected outcome on clean data; injected anomalies are recorded separately | Separates "expected decision" from "planted defect" |

## 3. Key decisions and trade-offs
* **Schema as single source of truth** (`schema_def.py`) → DDL, data dictionary, `schema.json`, validator all derive from it; docs cannot drift. Trade-off: a slightly unusual metadata format.
* **Simulate the workflow, don't sample tables.** POs → acks → changes → approvals → plan versions are produced by one event simulation with timestamps, so lifecycle consistency is a *consequence*, not something patched afterwards. Supersession (a new revision arriving before an approval finishes) falls out naturally and is a realistic exception path.
* **Derived values computed, never drawn** (line amounts, totals, ack status, latencies, deltas, lateness, shortfall, statuses from last event).
* **Downstream impact is computed** from the PO-line ↔ production-order bridge: supply is allocated to orders in start-date order; lateness = promise + 2-day buffer − start; shortfall = unallocated qty. This feeds escalation and decisions, so impact genuinely changes outcomes.
* **Injection after clean generation**, touching only the target row (+ the one directly dependent row), each logged with original/injected value and detection rule. Detectors in `validate.py` use *different* logic from the injector and never read ground truth; `evaluate.py` scores them.
* **Exceptions vs errors in validation.** Structural defects fail the build; the four business-rule violations are reported as EXCEPTIONS (expected), and arithmetic checks exclude rows knowingly affected by an exception to avoid cascading noise.
* **No wall clock, no hash-order dependence, one seeded RNG** → byte-identical reruns.
* **Frontend = static files + data snapshot** (no backend) so a reviewer can open it with zero setup. Trade-off: not live; the "Live replay" tab replays real logged events in accelerated time.

## 4. Testing approach and why I believe it works
1. **Generic schema-driven checks** (PK, FK, enum, regex, range, type, unique) on every table.
2. **Reconciliation checks** recompute totals/status/latency/deltas/impact from source rows.
3. **Lifecycle checks** replay `status_event` chains against an allowed-transition table and compare with snapshot columns.
4. **SQLite round-trip**: DDL (with CHECK/FK) + seed load, `PRAGMA foreign_key_check`, row counts equal CSV.
5. **Negative testing**: the four injected defects must be found with precision = recall = 1.0 (`evaluate.py`, pytest).
6. **Property tests** (`tests/`): exact event volume, only-approved-changes-touch-plan, revision contiguity, ground-truth separation, determinism, seed sensitivity.
7. **Reproducibility**: `repro_check.py` (two runs + control seed).
Honest development notes: the exclusions that stop an injected row cascading into arithmetic checks were designed up front, not discovered from failures; in self-review I found two placeholder checks that could never fail (D09, X12) and replaced/removed them; distributions were retuned once after the first full run showed too many MANAGER-level changes (slip shape and production slack).

## 5. Limitations (honest list)
* `evaluate.py` recomputes approval level/decision from the same documented rules as the generator; 100 % agreement shows **internal consistency, not real-world validity**.
* Distribution parameters (tier propensities, slack, slip shape, latencies) were tuned once for plausible mixes and are **not calibrated to real supplier data**. AUTO-level share is low (~9 %) because 52 % of acks match the request and create no change at all, and downstream impact escalates many others.
* `required_approval_level` / `level_driver` are rule-engine outputs stored in inputs – drop them if you train a model to predict approval.
* No goods receipt, shipments, FX, tolerance by supplier contract, or calendar/holiday logic beyond weekends.
* Injected anomalies are ~0.4 % of POs per type – good for detector testing, not a realistic base rate.
* Frontend shows a 60-change sample and a 667-event replay window, not all 10,000 POs; no authentication or write path.
* Data are fully synthetic; names are random combinations; no external data used (see `output/docs/source_manifest.json`).
