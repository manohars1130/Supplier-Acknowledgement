"""Single source of truth for the data model, enums and business rules.

Everything else (DDL, data_dictionary.md/csv, schema.json, validator, evaluator)
is generated from, or reads, this module's output - so docs can never drift from data.
"""

PO_STATUS = "ISSUED|NO_RESPONSE|ACKNOWLEDGED|CHANGE_PENDING|CONFIRMED|CHANGE_REJECTED|SUPPLIER_DECLINED"
CHG_STATUS = "SUBMITTED|PENDING_APPROVAL|APPROVED|APPLIED|REJECTED|SUPERSEDED"
ACK_STATUS = "ACCEPTED|ACCEPTED_WITH_CHANGES|PARTIALLY_ACCEPTED|REJECTED"
REASON_CODES = ("AUTO_WITHIN_TOLERANCE|WITHIN_PLANNER_AUTHORITY|DOWNSTREAM_LATE_GT_7D|"
                "DOWNSTREAM_SHORTFALL_GE_25PCT|ESCALATE_TO_MANAGER|ACCEPT_SUPPLIER_DECLINE|PRICE_GT_10PCT|"
                "DOWNSTREAM_LATE_GT_14D|MANAGER_APPROVED|SUPERSEDED_BY_NEW_REVISION|AWAITING_DECISION")
NOTIF_TYPES = ("ACK_DEVIATION_RECEIVED|APPROVAL_REQUIRED|CHANGE_APPLIED|CHANGE_REJECTED|"
               "DOWNSTREAM_IMPACT_ALERT|INBOUND_SCHEDULE_UPDATE|SUPPLIER_SILENCE|LOGISTICS_REVIEW_REQUESTED")

# Documented, parameterised business rules (also exported to docs/rules.json)
RULES = {
    "ack_sla_hours": 72,
    "inbound_buffer_days": 2,
    "approval_thresholds": {
        "note": "Deviations are measured on changed lines against the ORIGINAL PO request (cumulative), "
                "so repeated small slips cannot creep under a threshold. Highest triggered level wins.",
        "date_days_abs": {"auto_max": 2, "planner_max": 10},
        "qty_pct_abs": {"auto_max": 10, "planner_max": 40},
        "price_pct_abs": {"auto_max": 1, "planner_max": 5},
        "abs_value_impact_usd": {"planner_min": 5000, "manager_min": 25000},
    },
    "downstream_escalation": {
        "any_impacted_production_order_min_level": "PLANNER",
        "manager_if_lateness_days_gt": 10,
        "manager_if_shortfall_ratio_ge": 0.5,
    },
    "decision_rules": {
        "AUTO": "SYSTEM approves (AUTO_WITHIN_TOLERANCE)",
        "PLANNER": "REJECT if downstream lateness > 7d or shortfall ratio >= 0.25, else APPROVE",
        "MANAGER": "Planner step ESCALATED; manager REJECTS if max price deviation > 10% or lateness > 14d; "
                   "supplier full decline is ACCEPTED (re-source); else APPROVE",
        "planner_reject_if_lateness_gt": 7,
        "planner_reject_if_shortfall_ratio_ge": 0.25,
        "manager_reject_if_price_dev_gt": 10,
        "manager_reject_if_lateness_gt": 14,
    },
    "plan_update_rule": "inventory_plan gets a new version ONLY when the change's final approval step is APPROVED "
                        "(status APPLIED). REJECTED / PENDING / SUPERSEDED changes never touch the plan.",
}


def F(name, typ, req, key="", domain="", unit="", rule=""):
    return dict(name=name, type=typ, required=(req == "Y"), key=key, domain=domain, unit=unit, rule=rule)


def T(name, desc, cardinality, lifecycle, fields, constraints=()):
    return dict(name=name, desc=desc, cardinality=cardinality, lifecycle=lifecycle,
                fields=fields, constraints=list(constraints))


TABLES = [
    T("warehouse", "Receiving warehouse / plant (master).",
      "1:N purchase_order; 1:N app_user; 1:N production_order; 1:N inventory_plan",
      "Static master; effective_from only.",
      [F("warehouse_id", "TEXT", "Y", "PK", r"re:^WH-\d{2}$", "", "Immutable ID WH-NN"),
       F("warehouse_name", "TEXT", "Y", "UQ", "", "", "Unique display name"),
       F("region", "TEXT", "Y", "", "enum:NA|EMEA|APAC"),
       F("effective_from", "DATE", "Y", "", "", "ISO date", "Date the site went live")]),

    T("app_user", "People / system actors: planner, logistics analyst, warehouse (carrier-facing) operator, ops manager, SYSTEM.",
      "1:N purchase_order (planner); 1:N production_order (planner); 1:N approval; 1:N notification",
      "is_active flag; no deletes.",
      [F("user_id", "TEXT", "Y", "PK", r"re:^USR-\d{3}$", "", "USR-001 is the SYSTEM actor used for auto-approvals"),
       F("full_name", "TEXT", "Y", "", "", "", "Synthetic name (no real persons)"),
       F("role", "TEXT", "Y", "", "enum:PLANNER|LOGISTICS_ANALYST|WAREHOUSE_OPERATOR|OPS_MANAGER|SYSTEM"),
       F("warehouse_id", "TEXT", "N", "FK warehouse.warehouse_id", "", "", "NULL only for SYSTEM"),
       F("is_active", "BOOLEAN", "Y", "", "enum:0|1")]),

    T("supplier", "Supplier master with effective dating.",
      "1:N purchase_order",
      "ACTIVE -> ON_HOLD -> INACTIVE. New POs only to ACTIVE suppliers within [effective_from, effective_to].",
      [F("supplier_id", "TEXT", "Y", "PK", r"re:^SUP-\d{4}$"),
       F("supplier_name", "TEXT", "Y", "UQ"),
       F("country", "TEXT", "Y", "", "", "ISO-like country name"),
       F("reliability_tier", "TEXT", "Y", "", "enum:A|B|C", "", "A=most reliable. Drives deviation / silence propensity in the generator"),
       F("avg_lead_time_days", "INTEGER", "Y", "", "range:5..90", "days", "Typical order-to-requested-date lead time"),
       F("ack_sla_hours", "INTEGER", "Y", "", "range:1..240", "hours", "Contractual acknowledgement SLA (72)"),
       F("status", "TEXT", "Y", "", "enum:ACTIVE|ON_HOLD|INACTIVE"),
       F("effective_from", "DATE", "Y"),
       F("effective_to", "DATE", "N", "", "", "ISO date", "NULL = open ended; must be >= effective_from; set iff INACTIVE")]),

    T("sku", "Purchased item master.", "1:N po_line; 1:N inventory_plan", "Static master.",
      [F("sku_id", "TEXT", "Y", "PK", r"re:^SKU-\d{4}$"),
       F("description", "TEXT", "Y"),
       F("category", "TEXT", "Y", "", "enum:ELECTRONICS|MECHANICAL|PACKAGING|RAW_MATERIAL|CHEMICAL"),
       F("uom", "TEXT", "Y", "", "enum:EA|KG|L", "unit of measure"),
       F("standard_unit_price", "REAL", "Y", "", "range:0..", "USD per uom", "> 0")]),

    T("purchase_order", "PO header. Totals and status are DERIVED from po_line / status_event.",
      "1:N po_line; 1:N acknowledgement; 1:N change_request; 1:N status_event; 1:N notification; N:1 supplier",
      "ISSUED -> (NO_RESPONSE) -> ACKNOWLEDGED | CHANGE_PENDING -> CONFIRMED | CHANGE_REJECTED | SUPPLIER_DECLINED; "
      "re-entrant on later ack revisions. History in status_event; current_status is a snapshot of the last event.",
      [F("po_id", "TEXT", "Y", "PK", r"re:^PO-2026-\d{6}$", "", "Zero-padded, immutable, issued in order_date order"),
       F("supplier_id", "TEXT", "Y", "FK supplier.supplier_id"),
       F("planner_id", "TEXT", "Y", "FK app_user.user_id", "", "", "Role must be PLANNER"),
       F("receiving_warehouse_id", "TEXT", "Y", "FK warehouse.warehouse_id"),
       F("order_date", "DATE", "Y", "", "", "ISO date", "Business day within supplier effective window"),
       F("issued_ts", "TIMESTAMP", "Y", "", "", "UTC ISO-8601", "Date part == order_date"),
       F("currency", "TEXT", "Y", "", "enum:USD", "", "Single currency keeps thresholds unambiguous"),
       F("line_count", "INTEGER", "Y", "", "range:1..", "", "DERIVED = count(po_line)"),
       F("total_amount", "REAL", "Y", "", "range:0..", "USD", "DERIVED = sum(po_line.line_amount)"),
       F("ack_sla_due_ts", "TIMESTAMP", "Y", "", "", "UTC", "DERIVED = issued_ts + supplier.ack_sla_hours"),
       F("current_status", "TEXT", "Y", "", "enum:" + PO_STATUS, "", "DERIVED = to_status of last status_event"),
       F("latest_ack_revision", "INTEGER", "Y", "", "range:0..", "", "DERIVED = max(acknowledgement.revision_no), 0 if none")]),

    T("po_line", "PO line (requested values are immutable; changes live in change_request_line / inventory_plan).",
      "N:1 purchase_order; N:1 sku; 1:N acknowledgement_line; N:M production_order via production_order_po_line; 1:N inventory_plan",
      "Immutable after issue.",
      [F("po_line_id", "TEXT", "Y", "PK", r"re:^PO-2026-\d{6}-L\d{2}$"),
       F("po_id", "TEXT", "Y", "FK purchase_order.po_id"),
       F("line_no", "INTEGER", "Y", "", "range:1..99", "", "Unique within PO"),
       F("sku_id", "TEXT", "Y", "FK sku.sku_id", "", "", "No duplicate SKU within a PO"),
       F("requested_qty", "INTEGER", "Y", "", "range:1..", "sku.uom", "> 0"),
       F("unit_price", "REAL", "Y", "", "range:0..", "USD", "> 0"),
       F("line_amount", "REAL", "Y", "", "range:0..", "USD", "DERIVED = round(requested_qty*unit_price,2)"),
       F("requested_date", "DATE", "Y", "", "", "ISO date", ">= order_date; business day")],
      ["UNIQUE (po_id, line_no)"]),

    T("acknowledgement", "Supplier acknowledgement document; one row per revision (never overwritten).",
      "N:1 purchase_order; 1:N acknowledgement_line; 1:N change_request",
      "Immutable events. revision_no 1..n contiguous per PO; the highest revision is the latest.",
      [F("ack_id", "TEXT", "Y", "PK", r"re:^ACK-PO-2026-\d{6}-\d{2}$", "", "ACK-<po_id>-<revision 2 digits>"),
       F("po_id", "TEXT", "Y", "FK purchase_order.po_id"),
       F("revision_no", "INTEGER", "Y", "", "range:1..99", "", "Contiguous from 1 per PO"),
       F("received_ts", "TIMESTAMP", "Y", "", "", "UTC", "> PO issued_ts, strictly increasing by revision"),
       F("channel", "TEXT", "Y", "", "enum:EDI|PORTAL|EMAIL"),
       F("ack_status", "TEXT", "Y", "", "enum:" + ACK_STATUS, "",
         "DERIVED from lines vs ORIGINAL request: REJECTED if sum(accepted)=0; PARTIALLY_ACCEPTED if any accepted<requested; "
         "ACCEPTED_WITH_CHANGES if qty fully met but any price/date differs or qty uplift; else ACCEPTED"),
       F("total_accepted_qty", "INTEGER", "Y", "", "range:0..", "uom", "DERIVED = sum(line.accepted_qty)"),
       F("total_rejected_qty", "INTEGER", "Y", "", "range:0..", "uom", "DERIVED = sum(line.rejected_qty)"),
       F("response_latency_hours", "REAL", "Y", "", "range:0..", "hours", "DERIVED = received_ts - po.issued_ts"),
       F("is_late", "BOOLEAN", "Y", "", "enum:0|1", "", "DERIVED = latency > supplier.ack_sla_hours")],
      ["UNIQUE (po_id, revision_no)"]),

    T("acknowledgement_line", "Per-line accepted/rejected quantity, price and promised date within one acknowledgement.",
      "N:1 acknowledgement; N:1 po_line (must belong to the same PO); 1:0..1 promise_date; 1:0..1 change_request_line",
      "Immutable.",
      [F("ack_line_id", "TEXT", "Y", "PK", r"re:^ACK-PO-2026-\d{6}-\d{2}-L\d{2}$"),
       F("ack_id", "TEXT", "Y", "FK acknowledgement.ack_id"),
       F("po_line_id", "TEXT", "Y", "FK po_line.po_line_id", "", "", "po_line.po_id MUST equal acknowledgement.po_id"),
       F("accepted_qty", "INTEGER", "Y", "", "range:0..", "uom"),
       F("rejected_qty", "INTEGER", "Y", "", "range:0..", "uom", "DERIVED = max(0, po_line.requested_qty - accepted_qty)"),
       F("ack_unit_price", "REAL", "Y", "", "range:0..", "USD", "> 0"),
       F("promised_date", "DATE", "N", "", "", "ISO date", "NULL iff accepted_qty = 0; must be >= PO order_date"),
       F("rejection_reason", "TEXT", "N", "", "enum:CAPACITY|MATERIAL_SHORTAGE|MOQ|DISCONTINUED", "",
         "Required iff accepted_qty <> requested_qty (MOQ = uplift)")],
      ["UNIQUE (ack_id, po_line_id)"]),

    T("promise_date", "Effective-dated history of supplier promised dates per PO line (one row per ack_line with accepted_qty>0).",
      "N:1 po_line; 1:1 acknowledgement_line",
      "Row is_current=1 only for the latest revision; older rows are closed by valid_to_ts. Never updated in place by newer revisions.",
      [F("promise_id", "TEXT", "Y", "PK", r"re:^PRM-ACK-PO-2026-\d{6}-\d{2}-L\d{2}$", "", "PRM-<ack_line_id>"),
       F("po_line_id", "TEXT", "Y", "FK po_line.po_line_id"),
       F("ack_line_id", "TEXT", "Y", "UQ;FK acknowledgement_line.ack_line_id"),
       F("revision_no", "INTEGER", "Y", "", "range:1..99", "", "Copy of acknowledgement.revision_no"),
       F("promised_date", "DATE", "Y", "", "", "ISO date", "Equals acknowledgement_line.promised_date; >= order_date"),
       F("promised_qty", "INTEGER", "Y", "", "range:1..", "uom", "Equals accepted_qty"),
       F("received_ts", "TIMESTAMP", "Y", "", "", "UTC", "Ack receipt time"),
       F("applied_ts", "TIMESTAMP", "Y", "", "", "UTC", ">= received_ts; when the promise was loaded into the system"),
       F("valid_to_ts", "TIMESTAMP", "N", "", "", "UTC", "Time superseded by a later revision; NULL iff is_current"),
       F("is_current", "BOOLEAN", "Y", "", "enum:0|1", "", "Exactly one current row per line whose latest ack has accepted_qty>0, and it must be the max revision"),
       F("days_vs_requested", "INTEGER", "Y", "", "", "days", "DERIVED = promised_date - po_line.requested_date (+ = slip)")]),

    T("production_order", "Downstream production demand that consumes PO-line supply.",
      "N:M po_line via production_order_po_line; N:1 warehouse; N:1 app_user (planner)",
      "PLANNED (start in future) -> RELEASED (start <= as_of).",
      [F("production_order_id", "TEXT", "Y", "PK", r"re:^PRD-2026-\d{6}$"),
       F("warehouse_id", "TEXT", "Y", "FK warehouse.warehouse_id"),
       F("planner_id", "TEXT", "Y", "FK app_user.user_id", "", "", "Production planner; notified of downstream impact"),
       F("product_code", "TEXT", "Y", "", "", "", "Finished-good code (free text, synthetic)"),
       F("planned_qty", "INTEGER", "Y", "", "range:1..", "units"),
       F("planned_start_date", "DATE", "Y", "", "", "ISO date", ">= requested_date + inbound_buffer of every linked line at creation"),
       F("planned_end_date", "DATE", "Y", "", "", "ISO date", ">= planned_start_date"),
       F("status", "TEXT", "Y", "", "enum:PLANNED|RELEASED", "", "DERIVED from planned_start_date vs as_of")]),

    T("production_order_po_line", "Bridge: N:M between production_order and po_line with allocated component quantity.",
      "N:1 production_order; N:1 po_line", "Static allocation.",
      [F("production_order_id", "TEXT", "Y", "PK;FK production_order.production_order_id"),
       F("po_line_id", "TEXT", "Y", "PK;FK po_line.po_line_id"),
       F("allocated_qty", "INTEGER", "Y", "", "range:1..", "uom", "Sum over a po_line == po_line.requested_qty")]),

    T("change_request", "A deviation between an acknowledgement revision and the CURRENT approved plan. One per deviating ack.",
      "N:1 acknowledgement; N:1 purchase_order; 1:N change_request_line; 1:N approval; 1:N change_impact; 1:N inventory_plan (only when APPLIED)",
      "SUBMITTED -> PENDING_APPROVAL -> APPROVED -> APPLIED | REJECTED | SUPERSEDED (new ack revision arrives first).",
      [F("change_id", "TEXT", "Y", "PK", r"re:^CHG-PO-2026-\d{6}-\d{6}$", "", "CHG-<po_id>-<per-PO counter, 6 digits>"),
       F("po_id", "TEXT", "Y", "FK purchase_order.po_id"),
       F("ack_id", "TEXT", "Y", "FK acknowledgement.ack_id"),
       F("revision_no", "INTEGER", "Y", "", "range:1..99", "", "Revision of triggering ack"),
       F("submitted_ts", "TIMESTAMP", "Y", "", "", "UTC", "ack.received_ts + 1 minute"),
       F("has_qty_change", "BOOLEAN", "Y", "", "enum:0|1", "", "DERIVED: any line proposed_qty <> baseline_qty"),
       F("has_price_change", "BOOLEAN", "Y", "", "enum:0|1", "", "DERIVED"),
       F("has_date_change", "BOOLEAN", "Y", "", "enum:0|1", "", "DERIVED"),
       F("change_type", "TEXT", "Y", "", "enum:QTY|PRICE|DATE|MULTI", "", "DERIVED from has_* flags"),
       F("changed_line_count", "INTEGER", "Y", "", "range:1..", "", "DERIVED = count(change_request_line)"),
       F("max_qty_dev_pct", "REAL", "Y", "", "range:0..", "%", "DERIVED max abs qty deviation vs ORIGINAL request"),
       F("max_price_dev_pct", "REAL", "Y", "", "range:0..", "%", "DERIVED"),
       F("max_date_dev_days", "INTEGER", "Y", "", "range:0..", "days", "DERIVED max abs date deviation vs ORIGINAL request"),
       F("abs_value_impact", "REAL", "Y", "", "range:0..", "USD", "DERIVED = sum(abs(line value_delta))"),
       F("max_downstream_lateness_days", "INTEGER", "Y", "", "range:0..", "days", "DERIVED from change_impact (flagged rows)"),
       F("max_shortfall_ratio", "REAL", "Y", "", "range:0..1", "ratio", "DERIVED from change_impact: added shortfall / line requested_qty"),
       F("required_approval_level", "TEXT", "Y", "", "enum:AUTO|PLANNER|MANAGER", "", "Rule output (see rules.json)"),
       F("level_driver", "TEXT", "Y", "", "", "", "Pipe-separated rule codes that drove the level"),
       F("current_status", "TEXT", "Y", "", "enum:" + CHG_STATUS, "", "DERIVED = last change_request status_event"),
       F("resolved_ts", "TIMESTAMP", "N", "", "", "UTC", "APPLIED/REJECTED/SUPERSEDED time; NULL while pending")],
      ["UNIQUE (ack_id)"]),

    T("change_request_line", "Line-level old/new comparison: requested vs baseline (current plan) vs proposed (ack).",
      "N:1 change_request; N:1 po_line; N:1 acknowledgement_line; 1:N change_impact",
      "Immutable.",
      [F("change_line_id", "TEXT", "Y", "PK", r"re:^CHG-PO-2026-\d{6}-\d{6}-L\d{2}$"),
       F("change_id", "TEXT", "Y", "FK change_request.change_id"),
       F("po_line_id", "TEXT", "Y", "FK po_line.po_line_id"),
       F("ack_line_id", "TEXT", "Y", "FK acknowledgement_line.ack_line_id"),
       F("requested_qty", "INTEGER", "Y", "", "range:1..", "uom"),
       F("requested_unit_price", "REAL", "Y", "", "range:0..", "USD"),
       F("requested_date", "DATE", "Y"),
       F("baseline_qty", "INTEGER", "Y", "", "range:0..", "uom", "inventory_plan value in force at submission"),
       F("baseline_unit_price", "REAL", "Y", "", "range:0..", "USD"),
       F("baseline_date", "DATE", "Y"),
       F("proposed_qty", "INTEGER", "Y", "", "range:0..", "uom", "= ack_line.accepted_qty"),
       F("proposed_unit_price", "REAL", "Y", "", "range:0..", "USD", "= ack_line.ack_unit_price"),
       F("proposed_date", "DATE", "N", "", "", "", "NULL iff proposed_qty = 0"),
       F("qty_delta", "INTEGER", "Y", "", "", "uom", "DERIVED = proposed_qty - baseline_qty"),
       F("price_delta_pct", "REAL", "Y", "", "", "%", "DERIVED = (proposed - baseline)/baseline*100"),
       F("date_delta_days", "INTEGER", "Y", "", "", "days", "DERIVED = proposed_date - baseline_date (0 if NULL)"),
       F("qty_dev_pct_vs_requested", "REAL", "Y", "", "range:0..", "%", "DERIVED abs((proposed-requested)/requested*100)"),
       F("price_dev_pct_vs_requested", "REAL", "Y", "", "range:0..", "%", "DERIVED"),
       F("date_dev_days_vs_requested", "INTEGER", "Y", "", "range:0..", "days", "DERIVED abs days (0 if proposed_date NULL)"),
       F("value_delta", "REAL", "Y", "", "", "USD", "DERIVED = proposed_qty*proposed_price - baseline_qty*baseline_price")],
      ["UNIQUE (change_id, po_line_id)"]),

    T("approval", "Approval step events for a change (APR-<change>-<step>). Auto approvals are recorded by SYSTEM.",
      "N:1 change_request; N:1 app_user", "PENDING -> APPROVED | REJECTED | ESCALATED | WITHDRAWN.",
      [F("approval_id", "TEXT", "Y", "PK", r"re:^APR-CHG-PO-2026-\d{6}-\d{6}-\d{2}$"),
       F("change_id", "TEXT", "Y", "FK change_request.change_id"),
       F("step_no", "INTEGER", "Y", "", "range:1..9", "", "1 = first reviewer"),
       F("approver_role", "TEXT", "Y", "", "enum:SYSTEM|PLANNER|OPS_MANAGER"),
       F("approver_user_id", "TEXT", "Y", "FK app_user.user_id", "", "", "Role of user must match approver_role"),
       F("assigned_ts", "TIMESTAMP", "Y"),
       F("decided_ts", "TIMESTAMP", "N", "", "", "UTC", "NULL iff decision = PENDING; >= assigned_ts"),
       F("decision", "TEXT", "Y", "", "enum:PENDING|APPROVED|REJECTED|ESCALATED|WITHDRAWN"),
       F("reason_code", "TEXT", "Y", "", "enum:" + REASON_CODES)],
      ["UNIQUE (change_id, step_no)"]),

    T("change_impact", "Downstream impact: change line x linked production order (bridge-derived, computed from source rows).",
      "N:1 change_request; N:1 change_request_line; N:1 production_order",
      "Immutable, computed at submission.",
      [F("change_impact_id", "TEXT", "Y", "PK", r"re:^IMP-CHG-PO-2026-\d{6}-\d{6}-L\d{2}-\d{2}$"),
       F("change_id", "TEXT", "Y", "FK change_request.change_id"),
       F("change_line_id", "TEXT", "Y", "FK change_request_line.change_line_id"),
       F("production_order_id", "TEXT", "Y", "FK production_order.production_order_id"),
       F("impacted_planner_id", "TEXT", "Y", "FK app_user.user_id", "", "", "= production_order.planner_id"),
       F("planned_start_date", "DATE", "Y"),
       F("allocated_qty", "INTEGER", "Y", "", "range:1..", "uom"),
       F("baseline_lateness_days", "INTEGER", "Y", "", "range:0..", "days", "max(0, baseline_date + buffer - start) when covered"),
       F("proposed_lateness_days", "INTEGER", "Y", "", "range:0..", "days"),
       F("baseline_shortfall_qty", "INTEGER", "Y", "", "range:0..", "uom", "Supply allocated to orders in start-date order"),
       F("proposed_shortfall_qty", "INTEGER", "Y", "", "range:0..", "uom"),
       F("impact_flag", "BOOLEAN", "Y", "", "enum:0|1", "", "DERIVED = proposed_lateness > baseline_lateness OR proposed_shortfall > baseline_shortfall")],
      ["UNIQUE (change_line_id, production_order_id)"]),

    T("inventory_plan", "Projected supply per PO line, versioned. New version only after an APPROVED change.",
      "N:1 po_line; N:1 sku; N:1 warehouse; N:0..1 change_request; N:0..1 approval",
      "Version chain: valid_to_ts of v(n) == valid_from_ts of v(n+1); exactly one open (valid_to NULL) version per line.",
      [F("inventory_plan_id", "TEXT", "Y", "PK", r"re:^IPL-2026-\d{6}$"),
       F("po_line_id", "TEXT", "Y", "FK po_line.po_line_id"),
       F("sku_id", "TEXT", "Y", "FK sku.sku_id"),
       F("warehouse_id", "TEXT", "Y", "FK warehouse.warehouse_id"),
       F("plan_version", "INTEGER", "Y", "", "range:1..", "", "Contiguous from 1 per po_line"),
       F("projected_qty", "INTEGER", "Y", "", "range:0..", "uom"),
       F("projected_date", "DATE", "Y", "", "", "ISO date", "Expected receipt date (carried forward if qty = 0)"),
       F("unit_price", "REAL", "Y", "", "range:0..", "USD", "Agreed price in force"),
       F("valid_from_ts", "TIMESTAMP", "Y"),
       F("valid_to_ts", "TIMESTAMP", "N", "", "", "UTC", "NULL = current version"),
       F("source_change_id", "TEXT", "N", "FK change_request.change_id", "", "", "NULL iff version 1"),
       F("authorizing_approval_id", "TEXT", "N", "FK approval.approval_id", "", "", "Final APPROVED step; NULL iff version 1"),
       F("update_reason", "TEXT", "Y", "", "enum:INITIAL_PO|APPROVED_CHANGE")],
      ["UNIQUE (po_line_id, plan_version)"]),

    T("notification", "Planner / analyst / warehouse notifications raised by workflow events.",
      "N:1 app_user (recipient); N:1 purchase_order; N:0..1 change_request; N:0..1 production_order",
      "SENT -> READ.",
      [F("notification_id", "TEXT", "Y", "PK", r"re:^NOT-2026-\d{6}$", "", "Issued in created_ts order"),
       F("notification_type", "TEXT", "Y", "", "enum:" + NOTIF_TYPES),
       F("recipient_user_id", "TEXT", "Y", "FK app_user.user_id"),
       F("po_id", "TEXT", "Y", "FK purchase_order.po_id"),
       F("change_id", "TEXT", "N", "FK change_request.change_id"),
       F("production_order_id", "TEXT", "N", "FK production_order.production_order_id"),
       F("created_ts", "TIMESTAMP", "Y"),
       F("channel", "TEXT", "Y", "", "enum:EMAIL|IN_APP"),
       F("priority", "TEXT", "Y", "", "enum:LOW|NORMAL|HIGH"),
       F("status", "TEXT", "Y", "", "enum:SENT|READ"),
       F("read_ts", "TIMESTAMP", "N", "", "", "UTC", "NULL iff SENT; >= created_ts"),
       F("message", "TEXT", "Y")]),

    T("status_event", "Append-only lifecycle history for purchase_order and change_request (state changes are never overwritten).",
      "N:1 purchase_order; polymorphic entity_id -> purchase_order | change_request",
      "Append-only.",
      [F("status_event_id", "TEXT", "Y", "PK", r"re:^EVT-2026-\d{6}$", "", "Issued in event_ts order"),
       F("entity_type", "TEXT", "Y", "", "enum:PURCHASE_ORDER|CHANGE_REQUEST"),
       F("entity_id", "TEXT", "Y", "", "", "", "po_id or change_id depending on entity_type (validated by custom join check)"),
       F("po_id", "TEXT", "Y", "FK purchase_order.po_id"),
       F("event_seq", "INTEGER", "Y", "", "range:1..", "", "1..n per entity"),
       F("from_status", "TEXT", "N", "", "", "", "NULL only for event_seq = 1"),
       F("to_status", "TEXT", "Y", "", "enum:" + PO_STATUS + "|" + CHG_STATUS),
       F("event_ts", "TIMESTAMP", "Y"),
       F("trigger_id", "TEXT", "N", "", "", "", "ack_id / approval_id / change_id that caused the event")],
      ["UNIQUE (entity_type, entity_id, event_seq)"]),
]

TABLE_ORDER = [t["name"] for t in TABLES]

ALLOWED_PO_TRANSITIONS = {  # from -> allowed to (self-transitions are never emitted)
    "ISSUED": ["NO_RESPONSE", "ACKNOWLEDGED", "CHANGE_PENDING"],
    "NO_RESPONSE": ["ACKNOWLEDGED", "CHANGE_PENDING"],
    "ACKNOWLEDGED": ["CHANGE_PENDING", "CONFIRMED"],
    "CHANGE_PENDING": ["CONFIRMED", "CHANGE_REJECTED", "SUPPLIER_DECLINED", "ACKNOWLEDGED"],
    "CONFIRMED": ["CHANGE_PENDING", "ACKNOWLEDGED"],
    "CHANGE_REJECTED": ["CHANGE_PENDING", "ACKNOWLEDGED", "CONFIRMED"],
    "SUPPLIER_DECLINED": ["CHANGE_PENDING", "ACKNOWLEDGED", "CONFIRMED"],
}
ALLOWED_CHG_TRANSITIONS = {
    "SUBMITTED": ["PENDING_APPROVAL"],
    "PENDING_APPROVAL": ["APPROVED", "REJECTED", "SUPERSEDED"],
    "APPROVED": ["APPLIED", "SUPERSEDED"],
}

SQLTYPE = {"TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "REAL", "DATE": "TEXT", "TIMESTAMP": "TEXT", "BOOLEAN": "INTEGER"}


def key_tokens(f):
    return [k.strip() for k in f["key"].split(";") if k.strip()]


def ddl():
    out = ["-- Generated from schema_def.py. SQLite-compatible; run with PRAGMA foreign_keys=ON.", ""]
    for t in TABLES:
        cols, pks, fks = [], [], []
        for f in t["fields"]:
            c = f"  {f['name']} {SQLTYPE[f['type']]}"
            if f["required"]:
                c += " NOT NULL"
            d = f["domain"]
            if d.startswith("enum:"):
                vals = d[5:].split("|")
                lit = ",".join(v if f["type"] == "BOOLEAN" else "'" + v + "'" for v in vals)
                c += f" CHECK ({f['name']} IN ({lit}))"
            elif d.startswith("range:"):
                lo, hi = d[6:].split("..")
                conds = []
                if lo != "":
                    conds.append(f"{f['name']} >= {lo}")
                if hi != "":
                    conds.append(f"{f['name']} <= {hi}")
                if conds:
                    c += f" CHECK ({' AND '.join(conds)})"
            for k in key_tokens(f):
                if k == "PK":
                    pks.append(f["name"])
                elif k == "UQ":
                    c += " UNIQUE"
                elif k.startswith("FK "):
                    tbl, col = k[3:].split(".")
                    fks.append((f["name"], tbl, col))
            cols.append(c)
        body = cols[:]
        if pks:
            body.append(f"  PRIMARY KEY ({', '.join(pks)})")
        for c, tbl, col in fks:
            body.append(f"  FOREIGN KEY ({c}) REFERENCES {tbl}({col})")
        for extra in t["constraints"]:
            body.append("  " + extra)
        out.append(f"CREATE TABLE {t['name']} (\n" + ",\n".join(body) + "\n);\n")
    return "\n".join(out)


def data_dictionary_md():
    L = ["# Data dictionary", "",
         "Generated from `schema_def.py` (the same definitions produce the SQL DDL, `schema.json` and the validator).", "",
         "Conventions: timestamps are naive UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS`), dates ISO `YYYY-MM-DD`, booleans `0/1`, "
         "currency USD, empty CSV cell = NULL. **DERIVED** = computed from source rows, never drawn independently.", ""]
    for t in TABLES:
        L += [f"## {t['name']}", "", t["desc"], "",
              f"- **Cardinality / relationships:** {t['cardinality']}",
              f"- **Lifecycle / effective dating:** {t['lifecycle']}"]
        if t["constraints"]:
            L.append(f"- **Table constraints:** {'; '.join(t['constraints'])}")
        L += ["", "| Field | Type | Req | Key | Allowed values / format | Unit | Rule / notes |", "|---|---|---|---|---|---|---|"]
        for f in t["fields"]:
            d = f["domain"].replace("|", ", ").replace("enum:", "").replace("re:", "regex ").replace("range:", "range ")
            rule = f["rule"].replace("|", "/")
            L.append(f"| `{f['name']}` | {f['type']} | {'Y' if f['required'] else 'N'} | {f['key'].replace(';', ', ')} | {d} | {f['unit']} | {rule} |")
        L.append("")
    return "\n".join(L)
