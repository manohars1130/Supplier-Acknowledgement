# Prompt history

Tool: Claude (Anthropic) in the claude.ai chat interface, with its code-execution sandbox. One continuous conversation.
The prompts below are the user's messages in order, verbatim except Prompts 1 and 6, which are lightly condensed where noted (the assistant's replies are not reproduced here – export the full chat transcript from claude.ai and add it as `transcript/` before submitting if reviewers want the complete record).

---
### Prompt 1 – task brief 
```

Problem statement: Read it carefully. Your build should cover every point below.

Business Context / Problem
Manage supplier acknowledgements, promised dates, quantity changes and downstream schedule impacts.

Objective and Scope
Build a working solution for this end-to-end workflow: Manage supplier acknowledgements, promised dates, quantity changes and downstream schedule impacts. The solution should expose key decisions and exceptions, preserve traceability, and produce measurable outputs. Medium complexity means multiple related entities, conditional rules, at least one exception path and meaningful evaluation metrics.

Primary Actors / Systems
Supplier, planner, carrier/warehouse operator, logistics analyst and operations manager

Business Workflow and Rules
1. Generate/reference master data first: Supplier, PurchaseOrder, POLine, Acknowledgement, PromiseDate.
2. Generate dependent transaction/event records using valid parent IDs.
3. Apply business logic: Create POs and lines; generate acknowledgements with accepted/rejected quantities and promised dates; compare changes with requested values; apply approval thresholds for price/date/quantity changes; update projected supply only after approval; notify affected planners.
4. Persist state changes as explicit records/events where relevant; do not overwrite history.
5. Generate outcomes and ground-truth labels from documented rules, not independent random assignment.

Entities and Relationships
Core entities: Supplier, PurchaseOrder, POLine, Acknowledgement, PromiseDate, ChangeRequest, Approval, InventoryPlan, ProductionOrder, Notification. Define a primary key for every entity and foreign keys for every relationship. Document cardinality (1:1, 1:N, N:M), optional relationships, effective dates and lifecycle states. Use bridge tables for many-to-many relationships; retain event history for state changes; validate all joins. For deliberate anomaly cases, keep an issue_type/ground_truth label rather than silently corrupting unrelated rows.

ID / Nomenclature Conventions
PO-2026-000001; ACK-<PO-id>-01; CHG-<PO-id>-000001; APR-<change-id>-01; NOT-2026-000001.

Synthetic Data Generation Instructions (Granular)
Step 1 — Write a data dictionary for: Supplier, PurchaseOrder, POLine, Acknowledgement, PromiseDate, ChangeRequest, Approval, InventoryPlan, ProductionOrder, Notification. For each field specify type, required/optional, allowed values, units, uniqueness and validation rule.
Step 2 — Create parent/master tables first, then child and event tables. Apply this domain-specific generation logic: [same business logic as above].
Step 3 — Apply deterministic IDs and naming: [ID conventions above]. Keep counters zero-padded, IDs immutable, and referenced IDs exactly consistent across files.
Step 4 — Generate the baseline volume: Generate 10,000 POs and 15,000 acknowledgement/change events. Include partial acceptance, date slips, price changes, supplier silence and multiple revisions. Use a fixed seed (e.g. 20261022) and parameterize scale so starter and full datasets use the same generator.
Step 5 — Generate valid baseline records first, then inject labelled edge cases: Unapproved change updates inventory plan; acknowledgement refers to wrong PO line; promised date before order date; older revision overwrites newer revision.
Step 6 — Calculate derived values (totals, balances, scores, statuses and dates) from source records rather than generating them independently.
Step 7 — Produce a separate ground_truth.csv/json for known anomalies, expected decisions or labels; never leak it into input features.
Step 8 — Run validation: primary-key uniqueness, foreign-key integrity, allowed enums, date ordering, arithmetic reconciliation, lifecycle transitions and duplicate checks.
Step 9 — Export CSV/JSON/SQL seed data plus README, schema/data dictionary, generator command, fixed seed, source manifest and validation summary.
Step 10 — Regenerate twice with the same seed and compare row counts/checksums to demonstrate reproducibility.

Suggested Volume and Coverage: 10,000 POs and 15,000 acknowledgement/change events; partial acceptance, date slips, price changes, supplier silence, multiple revisions.
Required Edge Cases / Negative Scenarios: Unapproved change updates inventory plan; acknowledgement refers to wrong PO line; promised date before order date; older revision overwrites newer revision.
Validation Rules / Acceptance Criteria: Revision history is preserved; only approved changes affect plans; downstream impact is identified; acknowledgement status is consistent.
Evaluation Focus for Generated Data: (a) realistic distributions and meaningful variation; (b) consistent IDs and valid joins; (c) dependency/lifecycle consistency across entities; (d) correct derived values and reconciliation; (e) normal, boundary, failure and exception coverage; (f) separate ground truth; (g) fixed-seed reproducibility, documentation and lawful provenance.
Public Data Guidance / Lawful Use: External data is optional. If used, record source URL, dataset/version, retrieval date, license/terms, attribution, transformations and fields discarded. [...] If licensing or suitability is unclear, use synthetic data and cite public reference data only for schema inspiration.
```
(The full text was pasted; the Step 2 / Step 3 / Suggested-volume / Required-edge-case sections repeat the rules shown in the "Business Workflow", "ID conventions" and Step 4–5 text, so they are abbreviated with `[...]` here only.)

### Prompt 2
`I decided to add a frontend so the generated supplier acknowledgement data could be explored in a dashboard. 
I worked on connecting the frontend to the project’s output data and reviewed the displayed information to make sure it matched the project’s purpose. I used AI for help with frontend implementation.
`

### Prompt 3
`I wanted the dashboard to be easier to read and use, so I added styling and JavaScript behavior. 
I reviewed the changes in the browser and adjusted the result based on how the page looked and behaved. AI helped me with implementation ideas and code.
`

### Prompt 4
`I needed the frontend files to work together as part of the project. 
I combined the relevant files and checked that the dashboard could load. I used AI for help resolving integration issues, then ran the project myself.
`

### Prompt 5 (with a screenshot of the dashboard attached)
`I wanted the dashboard to look clearer and feel more useful for following project activity. 
I asked for help improving the background and showing the available event data in a more live-looking view. The project displays generated data; it does not connect to a live supplier system unless a real-time data source is added.
`

### Prompt 6 – this submission step
```
I ran the project locally, generated the frontend data, and started the dashboard with Python’s HTTP server. 
I used AI to troubleshoot command-line errors and clarify the run steps. I reviewed the commands and outputs myself. Before submission, I will run the documented setup and verification steps and report any limitations honestly.

```
