<<<<<<< HEAD
# Supplier Acknowledgement, Promise-Date & Change-Impact Platform

Synthetic data generator + validator + evaluator + dashboard for the workflow
**"manage supplier acknowledgements, promised dates, quantity changes and downstream schedule impacts"**
(PO → acknowledgement revisions → change request → tiered approval → inventory-plan update → planner notifications).

* Reasoning, assumptions, trade-offs, limitations → [`DESIGN.md`](DESIGN.md)
* How AI was used (and what was/wasn't verified by a human) → [`AI_USAGE.md`](AI_USAGE.md)
* Prompts used → [`PROMPT_HISTORY.md`](PROMPT_HISTORY.md)

## 1. Requirements
Python 3.10+ (tested 3.12), `make` (optional – every target is just a `python3 …` command, listed below), a browser. No database server, no API keys, no network access needed at run time.

## 2. Install
```bash
git clone <your-repo-url> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt                          # pandas, numpy, pytest
```
No configuration is required. All parameters are CLI flags with the task's defaults (seed `20261022`, 10,000 POs, 15,000 ack/change events, as-of `2026-10-22T18:00:00`).

## 3. Run
| Goal | Command | Time |
|---|---|---|
| **Everything** (generate → validate → evaluate → reproducibility → rebuild dashboard data) | `make all` | ~1 min |
| Tests only (small datasets in temp dirs) | `make test` | ~3 s |
| Small "starter" dataset, same generator | `make starter` | ~1 s |
| Dashboard | `make serve` → open http://localhost:8000 | – |

Without `make`:
```bash
python3 generate_data.py --seed 20261022 --n-pos 10000 --n-events 15000 --out output
python3 validate.py  --root output
python3 evaluate.py  --root output
python3 repro_check.py
python3 tools/build_frontend_data.py --root output --out frontend/data.js
python3 -m pytest -q
cd frontend && python3 -m http.server 8000
```
The dashboard (`frontend/`) also works by simply opening `frontend/index.html` – a snapshot of the data is committed in `frontend/data.js`.

## 4. How to verify key behaviour
| Claim | Verify with | Expected |
|---|---|---|
| Volume: 10,000 POs, exactly 15,000 ack+change events | `make all`, then `cat output/docs/generation_manifest.json` (row_counts) | purchase_order 10000; acknowledgement + change_request = 15000 |
| Structural integrity (PK/FK/enums/dates/arithmetic/lifecycle/duplicates) | `python3 validate.py --root output` | `OVERALL: PASS \| ERROR checks failed: 0` (53 checks incl. SQLite `PRAGMA foreign_key_check`) |
| Only approved changes affect the plan | `pytest -q -k approved` and validator rule `X04` | plan rows from non-APPLIED changes == exactly the 40 labelled injections |
| 4 required edge cases are detectable | `python3 evaluate.py --root output` → `exception_detection_vs_ground_truth` | precision = recall = 1.0 for each type |
| Revision history preserved | validator `D03`, `L06`, `L08`; `pytest -k history` | pass |
| Ground truth not leaked into inputs | `pytest -k separate` | no label columns in `output/data/*.csv`; labels only in `output/ground_truth/` |
| Reproducible | `python3 repro_check.py` (exit code 0) | identical row counts + SHA-256 for all files across 2 runs; a different seed changes them |
| Business rules in SQL | `python3 -c "import sqlite3;c=sqlite3.connect(':memory:');c.executescript(open('output/sql/schema.sql').read());c.executescript(open('output/sql/seed_data.sql').read());print(c.execute('select required_approval_level,count(*) from change_request group by 1').fetchall())"` | AUTO / PLANNER / MANAGER counts |

Expected headline numbers for the default seed are in `output/docs/metrics_report.json` after `make all` (e.g. ~7 % POs silent past SLA, ~89 % of resolved changes approved).

## 5. Repository map
```
schema_def.py        data model (18 tables), enums, business RULES, DDL + data-dictionary generators
generate_data.py     seeded workflow simulator, anomaly injection, CSV/JSON/SQL export, manifests
validate.py          independent validator (reads data/ + docs/ only, never ground truth)
evaluate.py          KPIs, detector precision/recall vs ground truth, independent rule recomputation
repro_check.py       regenerate twice + control seed, compare checksums
tools/build_frontend_data.py   dataset → frontend/data.js
frontend/            index.html, styles.css, app.js, data.js (overview, change queue, exceptions, live replay)
tests/               pytest end-to-end behaviour tests
output/              (git-ignored) generated: data/ ground_truth/ sql/ docs/ (dictionary, rules, manifests, reports)
```
Generated documentation (field-level data dictionary with types / required / allowed values / units / uniqueness / validation) is written to `output/docs/data_dictionary.md|csv` and `schema.json`.

## 6. Data model (summary)
Entities: Supplier, PurchaseOrder, POLine, Acknowledgement(+Line), PromiseDate, ChangeRequest(+Line), Approval, InventoryPlan, ProductionOrder (N:M to POLine via bridge), Notification, plus warehouse / user / sku masters, `change_impact` and an append-only `status_event` log.
IDs: `PO-2026-000001`, `ACK-<PO>-01`, `CHG-<PO>-000001`, `APR-<CHG>-01`, `NOT-2026-000001`.
Cardinalities and lifecycles per table: `output/docs/data_dictionary.md`.
=======
# Supplier-Acknowledgement
Synthetic data generator, validator and dashboard for supplier acknowledgements, promise dates, change approvals and downstream schedule impact. Seeded, reproducible, with labelled edge cases and separate ground truth.
>>>>>>> 8e44d4bdd9545fcc67f84c88dda80d2896492180
