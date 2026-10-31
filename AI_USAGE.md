# AI usage statement

> Sections marked **[YOU: …]** can only be answered truthfully by the person submitting. Edit them before submitting; do not leave claims you cannot stand behind.

## 1. Tool
Claude (Anthropic) via claude.ai with a sandboxed code-execution environment. Prompts: see `PROMPT_HISTORY.md`.

## 2. Where AI did the work (heavy reliance)
Be explicit: **the assistant wrote essentially all of the code and prose in this repository** from the challenge brief, in one conversation:
* data model and rules (`schema_def.py`), the generator and its workflow simulation (`generate_data.py`), validator, evaluator, reproducibility script, tests, Makefile
* the dashboard (HTML/CSS/JS), the live-replay tab, `tools/build_frontend_data.py`
* README, DESIGN, this file

## 3. Where AI accelerated most
* Turning a long, rule-heavy brief into a consistent 18-table model with documented cardinalities and a machine-readable dictionary.
* Designing the event-driven simulation (supersession of in-flight approvals, plan versioning, exact-volume revision loop).
* Writing the broad validator and the injected-anomaly detectors, and iterating after test runs.

## 4. What the assistant itself verified by running code in its sandbox
(Observed output, not human review.)
* `make all` on a clean checkout: generation (≈10 s), `validate.py` 53 checks / 0 ERROR, `evaluate.py` detector precision = recall = 1.0 for all four anomaly types, `repro_check.py` identical SHA-256 across two runs (41 files) and different for a different seed.
* `pytest`: 8 passed.
* Frontend: JavaScript syntax check and a jsdom smoke test (tabs render, change drill-down opens, live replay fills the feed and counters). **No test in a real browser/screenshot of the final design was performed by the assistant.**
* Self-review fixed two placeholder validator checks that could never fail.

## 5. What a human must still review  **[YOU: complete]**
* [YOU: Which parts of the rules (thresholds in `schema_def.RULES`, decision rules) did you read and agree with as realistic? Did you change any?]
* [YOU: Which generated files / dashboard views did you open and inspect? What did you notice?]
* [YOU: Anything you changed after the AI's draft (code, thresholds, wording)? List it – ideally as separate commits.]
* [YOU: Run `make all` and `make test` yourself on your machine; record the date / OS / Python version here.]

## 6. Known weak spots to scrutinise
* Evaluator "rule recomputation = 100 %" is a consistency check against the *same* rules, not independent validation.
* Distribution parameters are plausible, not calibrated to real data.
* Commit history was assembled **after** the work (logical grouping of final files), not captured live while editing, so it shows structure rather than the true chronological evolution. Real chronology is in the chat transcript.
