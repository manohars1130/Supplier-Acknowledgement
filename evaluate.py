#!/usr/bin/env python3
"""Evaluation: (1) business KPIs, (2) exception-detector precision/recall vs ground truth,
(3) independent recomputation of approval level / decision from input columns vs ground-truth labels.

Ground truth is read ONLY here, never by the generator's inputs or by validate.py.
    python3 evaluate.py --root .
"""
import argparse, json
import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    r = ap.parse_args().root
    rd = lambda n: pd.read_csv(f"{r}/data/{n}.csv", keep_default_na=True)
    po, pl, ack, al, pr = rd("purchase_order"), rd("po_line"), rd("acknowledgement"), rd("acknowledgement_line"), rd("promise_date")
    chg, cl, ap_, imp, plan, note = rd("change_request"), rd("change_request_line"), rd("approval"), rd("change_impact"), rd("inventory_plan"), rd("notification")
    ev = rd("status_event")
    rules = json.load(open(f"{r}/docs/rules.json"))
    gt = pd.read_csv(f"{r}/ground_truth/ground_truth_anomalies.csv", keep_default_na=False)
    lab = pd.read_csv(f"{r}/ground_truth/ground_truth_labels.csv", keep_default_na=False, dtype=str)
    det = pd.read_csv(f"{r}/docs/detected_exceptions.csv")
    M = {}

    # ------------------------------------------------------------ 1. business KPIs
    n_po = len(po)
    first = ack[ack.revision_no == 1]
    M["volume"] = dict(purchase_orders=n_po, po_lines=len(pl), acknowledgements=len(ack), change_requests=len(chg),
                       ack_plus_change_events=len(ack) + len(chg), avg_lines_per_po=round(len(pl) / n_po, 2))
    never = ~po.po_id.isin(set(ack.po_id))
    past_sla = po.ack_sla_due_ts <= "2026-10-22T18:00:00"
    M["supplier_responsiveness"] = dict(
        pct_pos_with_any_ack=round(100 * (1 - never.mean()), 2),
        pct_pos_silent_past_sla=round(100 * (never & past_sla).sum() / max(1, past_sla.sum()), 2),
        pct_first_ack_late=round(100 * first.is_late.mean(), 2),
        median_first_ack_latency_h=round(first.response_latency_hours.median(), 1),
        p90_first_ack_latency_h=round(first.response_latency_hours.quantile(.9), 1),
        pct_pos_with_multiple_revisions=round(100 * (po.latest_ack_revision >= 2).mean(), 2),
        max_revisions=int(po.latest_ack_revision.max()))
    M["ack_status_mix_pct"] = (ack.ack_status.value_counts(normalize=True) * 100).round(2).to_dict()
    M["ack_status_by_supplier_tier_pct_deviating"] = None
    tier = po.merge(rd("supplier")[["supplier_id", "reliability_tier"]], on="supplier_id")
    t1 = first.merge(tier[["po_id", "reliability_tier"]], on="po_id")
    M["ack_status_by_supplier_tier_pct_deviating"] = (t1.groupby("reliability_tier").ack_status.apply(lambda s: round(100 * (s != "ACCEPTED").mean(), 2))).to_dict()
    M["po_current_status_mix_pct"] = (po.current_status.value_counts(normalize=True) * 100).round(2).to_dict()
    M["change_type_mix_pct"] = (chg.change_type.value_counts(normalize=True) * 100).round(2).to_dict()
    M["approval_level_mix_pct"] = (chg.required_approval_level.value_counts(normalize=True) * 100).round(2).to_dict()
    M["change_status_mix_pct"] = (chg.current_status.value_counts(normalize=True) * 100).round(2).to_dict()
    resolved = chg[chg.current_status.isin(["APPLIED", "REJECTED"])]
    M["approval_outcomes"] = dict(
        approval_rate_of_resolved_pct=round(100 * (resolved.current_status == "APPLIED").mean(), 2),
        rejection_rate_of_resolved_pct=round(100 * (resolved.current_status == "REJECTED").mean(), 2),
        superseded_pct_of_all=round(100 * (chg.current_status == "SUPERSEDED").mean(), 2),
        still_pending_pct_of_all=round(100 * (chg.current_status == "PENDING_APPROVAL").mean(), 2))
    cyc = resolved.assign(h=(pd.to_datetime(resolved.resolved_ts) - pd.to_datetime(resolved.submitted_ts)).dt.total_seconds() / 3600)
    M["approval_cycle_time_h_median_by_level"] = cyc.groupby("required_approval_level").h.median().round(1).to_dict()
    M["date_slip_days_vs_requested"] = dict(
        pct_promises_slipped=round(100 * (pr.days_vs_requested > 0).mean(), 2), pct_pulled_in=round(100 * (pr.days_vs_requested < 0).mean(), 2),
        mean_slip_days_when_slipped=round(pr[pr.days_vs_requested > 0].days_vs_requested.mean(), 2),
        p95_slip_days=float(pr.days_vs_requested.quantile(.95)))
    M["partial_acceptance"] = dict(pct_ack_lines_short_accepted=round(100 * (al.rejected_qty > 0).mean(), 2),
                                   pct_ack_lines_fully_rejected=round(100 * (al.accepted_qty == 0).mean(), 2))
    M["price_changes"] = dict(pct_change_lines_with_price_change=round(100 * (cl.price_delta_pct.abs() > 0).mean(), 2),
                              mean_price_increase_pct=round(cl[cl.price_delta_pct > 0].price_delta_pct.mean(), 2))
    fl = imp[imp.impact_flag == 1]
    M["downstream_impact"] = dict(
        pct_changes_with_impacted_production_orders=round(100 * chg.change_id.isin(set(fl.change_id)).mean(), 2),
        impacted_production_orders=int(fl.production_order_id.nunique()),
        mean_lateness_days_flagged=round(fl.proposed_lateness_days.mean(), 2),
        total_added_shortfall_units=int((fl.proposed_shortfall_qty - fl.baseline_shortfall_qty).clip(lower=0).sum()))
    # notification coverage: every APPLIED change with flagged impact notified each impacted planner
    app = chg[chg.current_status == "APPLIED"]
    need = fl[fl.change_id.isin(set(app.change_id))][["change_id", "impacted_planner_id"]].drop_duplicates()
    have = note[note.notification_type == "DOWNSTREAM_IMPACT_ALERT"][["change_id", "recipient_user_id"]].drop_duplicates()
    cov = need.merge(have, left_on=["change_id", "impacted_planner_id"], right_on=["change_id", "recipient_user_id"], how="left", indicator=True)
    M["notification_coverage"] = dict(impacted_planner_alerts_required=len(need),
                                      coverage_pct=round(100 * (cov._merge == "both").mean(), 2) if len(need) else 100.0,
                                      notifications_total=len(note), notifications_by_type=note.notification_type.value_counts().to_dict(),
                                      pct_read=round(100 * (note.status == "READ").mean(), 2))
    M["plan_integrity"] = dict(plan_versions=len(plan), versions_from_changes=int(plan.source_change_id.notna().sum()),
                               pct_changes_applied_to_plan=round(100 * len(app) / len(chg), 2))

    # ------------------------------------------------------------ 2. exception detection vs ground truth
    sc = {}
    for it, g in gt.groupby("issue_type"):
        truth = set(g.record_id)
        pred = set(det[det.issue_type == it].record_id)
        tp, fp, fn = len(truth & pred), len(pred - truth), len(truth - pred)
        p = tp / (tp + fp) if tp + fp else 1.0
        rc = tp / (tp + fn) if tp + fn else 1.0
        sc[it] = dict(injected=len(truth), detected=len(pred), tp=tp, fp=fp, fn=fn, precision=round(p, 4), recall=round(rc, 4),
                      f1=round(2 * p * rc / (p + rc), 4) if p + rc else 0.0)
    M["exception_detection_vs_ground_truth"] = sc

    # ------------------------------------------------------------ 3. rule recomputation from INPUT columns
    th, dn, dr = rules["approval_thresholds"], rules["downstream_escalation"], rules["decision_rules"]
    c = chg.copy()
    lvl = np.zeros(len(c), dtype=int)

    def bump(mask, v):
        nonlocal lvl
        lvl = np.where(mask, np.maximum(lvl, v), lvl)
    bump(c.max_date_dev_days > th["date_days_abs"]["auto_max"], 1); bump(c.max_date_dev_days > th["date_days_abs"]["planner_max"], 2)
    bump(c.max_qty_dev_pct > th["qty_pct_abs"]["auto_max"], 1); bump(c.max_qty_dev_pct > th["qty_pct_abs"]["planner_max"], 2)
    bump(c.max_price_dev_pct > th["price_pct_abs"]["auto_max"], 1); bump(c.max_price_dev_pct > th["price_pct_abs"]["planner_max"], 2)
    bump(c.abs_value_impact >= th["abs_value_impact_usd"]["planner_min"], 1); bump(c.abs_value_impact >= th["abs_value_impact_usd"]["manager_min"], 2)
    flagged = c.change_id.isin(set(fl.change_id))
    bump(flagged, 1)
    bump(flagged & ((c.max_downstream_lateness_days > dn["manager_if_lateness_days_gt"]) | (c.max_shortfall_ratio >= dn["manager_if_shortfall_ratio_ge"])), 2)
    c["re_level"] = np.array(["AUTO", "PLANNER", "MANAGER"])[lvl]
    declined = c.ack_id.isin(set(ack[ack.total_accepted_qty == 0].ack_id))
    d = np.where(c.re_level == "AUTO", "APPROVED",
        np.where(c.re_level == "PLANNER",
                 np.where((c.max_downstream_lateness_days > dr["planner_reject_if_lateness_gt"]) | (c.max_shortfall_ratio >= dr["planner_reject_if_shortfall_ratio_ge"]), "REJECTED", "APPROVED"),
                 np.where(declined, "APPROVED", np.where((c.max_price_dev_pct > dr["manager_reject_if_price_dev_gt"]) | (c.max_downstream_lateness_days > dr["manager_reject_if_lateness_gt"]), "REJECTED", "APPROVED"))))
    c["re_decision"] = d
    L = lab.pivot_table(index="entity_id", columns="label_name", values="label_value", aggfunc="first")
    c = c.merge(L, left_on="change_id", right_index=True, how="left")
    resolved_c = c[c.FINAL_DECISION.isin(["APPROVED", "REJECTED"])]
    M["rule_recomputation"] = dict(
        approval_level_accuracy_vs_ground_truth=round(100 * (c.re_level == c.APPROVAL_LEVEL).mean(), 3),
        approval_level_accuracy_vs_recorded_column=round(100 * (c.re_level == c.required_approval_level).mean(), 3),
        final_decision_accuracy_vs_ground_truth_on_resolved=round(100 * (resolved_c.re_decision == resolved_c.FINAL_DECISION).mean(), 3),
        n_changes=len(c), n_resolved=len(resolved_c))
    ps = lab[lab.label_name == "PO_STATUS"].set_index("entity_id").label_value
    M["rule_recomputation"]["po_status_matches_ground_truth_pct"] = round(100 * (po.set_index("po_id").current_status == ps.reindex(po.po_id)).mean(), 3)
    sil = lab[lab.label_name == "SUPPLIER_SILENCE_EXCEPTION"]
    M["rule_recomputation"]["silence_exceptions_labelled"] = int((sil.label_value == "1").sum())
    out = json.dumps(M, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    open(f"{r}/docs/metrics_report.json", "w").write(out)
    print(out)


if __name__ == "__main__":
    main()
