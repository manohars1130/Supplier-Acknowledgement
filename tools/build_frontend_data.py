#!/usr/bin/env python3
"""Build frontend/data.js (metrics + sample changes + exception examples + event replay log) from a generated dataset.
    python3 tools/build_frontend_data.py --root output --out frontend/data.js
"""
import argparse, json
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="output"); ap.add_argument("--out", default="frontend/data.js")
ap.add_argument("--t0", default="2026-10-20T18:00:00", help="replay window start (36h before as-of)")
a = ap.parse_args(); r = a.root
rd = lambda n: pd.read_csv(f"{r}/data/{n}.csv")
m = json.load(open(f"{r}/docs/metrics_report.json"))
chg, cl, ap_, imp, po, sup, ev = (rd(n) for n in ["change_request", "change_request_line", "approval", "change_impact", "purchase_order", "supplier", "status_event"])
chg = chg.merge(po[["po_id", "supplier_id"]], on="po_id").merge(sup[["supplier_id", "reliability_tier"]], on="supplier_id")
sel = pd.concat([chg[chg.required_approval_level == l].groupby("current_status").head(k) for l, k in [("MANAGER", 8), ("PLANNER", 8), ("AUTO", 3)]]).drop_duplicates("change_id").head(60)
C = []
for _, c in sel.iterrows():
    i = c.change_id
    C.append(dict(id=i, po=c.po_id, sup=c.supplier_id, tier=c.reliability_tier, lvl=c.required_approval_level, st=c.current_status, why=c.level_driver, ts=c.submitted_ts[:16],
                  val=c.abs_value_impact, late=int(c.max_downstream_lateness_days),
                  L=[[x.po_line_id[-3:], x.requested_qty, x.baseline_qty, x.proposed_qty, x.requested_unit_price, x.proposed_unit_price, x.requested_date, x.proposed_date if isinstance(x.proposed_date, str) else "—"] for x in cl[cl.change_id == i].itertuples()],
                  A=[[x.approver_role, x.decision, x.reason_code, str(x.decided_ts)[:16]] for x in ap_[ap_.change_id == i].itertuples()],
                  I=[[x.production_order_id, x.proposed_lateness_days, x.proposed_shortfall_qty - x.baseline_shortfall_qty] for x in imp[(imp.change_id == i) & (imp.impact_flag == 1)].itertuples()]))
g = pd.read_csv(f"{r}/ground_truth/ground_truth_anomalies.csv", keep_default_na=False)
E = {k: dict(n=len(v), rule=v.detection_rule.iloc[0], ex=v.head(4)[["record_id", "po_id", "original_value", "injected_value"]].values.tolist()) for k, v in g.groupby("issue_type")}
keys = ["volume", "supplier_responsiveness", "ack_status_mix_pct", "approval_level_mix_pct", "change_status_mix_pct", "approval_outcomes",
        "approval_cycle_time_h_median_by_level", "downstream_impact", "notification_coverage", "exception_detection_vs_ground_truth", "ack_status_by_supplier_tier_pct_deviating"]
M = {k: m[k] for k in keys}; M["notification_coverage"].pop("notifications_by_type", None)
t0 = pd.Timestamp(a.t0)
ev["t"] = pd.to_datetime(ev.event_ts)
w = ev[ev.t >= t0].sort_values("t")
MAP = {("PURCHASE_ORDER", "ISSUED"): "PI", ("PURCHASE_ORDER", "NO_RESPONSE"): "NR", ("PURCHASE_ORDER", "ACKNOWLEDGED"): "AK", ("PURCHASE_ORDER", "CHANGE_PENDING"): "AD",
       ("PURCHASE_ORDER", "CONFIRMED"): "CF", ("PURCHASE_ORDER", "CHANGE_REJECTED"): "PJ", ("PURCHASE_ORDER", "SUPPLIER_DECLINED"): "SD",
       ("CHANGE_REQUEST", "SUBMITTED"): "CS", ("CHANGE_REQUEST", "APPLIED"): "CA", ("CHANGE_REQUEST", "REJECTED"): "CR", ("CHANGE_REQUEST", "SUPERSEDED"): "CX"}
sm = po.set_index("po_id").supplier_id
w = w[[(x, y) in MAP for x, y in zip(w.entity_type, w.to_status)]]
EV = [[int((x.t - t0).total_seconds() // 60), MAP[(x.entity_type, x.to_status)], x.entity_id[-13:] if x.entity_type == "CHANGE_REQUEST" else x.entity_id, sm[x.po_id]] for x in w.itertuples()]
D = dict(m=M, C=C, E=E, ev=EV, t0=a.t0)
open(a.out, "w").write("// Snapshot of the generated dataset (metrics + sample changes + exception examples + event replay log).\nwindow.DATA=" + json.dumps(D, separators=(",", ":"), default=str) + ";\n")
print(f"wrote {a.out}: {len(C)} sample changes, {len(EV)} replay events")
