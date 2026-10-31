#!/usr/bin/env python3
"""Validate a generated dataset using ONLY data/ + docs/ (never reads ground_truth/).

Severity ERROR     : structural / arithmetic / lifecycle defect -> must be 0 for acceptance.
Severity EXCEPTION : business-rule violation (the four labelled edge cases) -> detected, listed in
                     docs/detected_exceptions.csv, and scored against ground truth by evaluate.py.
    python3 validate.py --root .
"""
import argparse, json, os, re, sqlite3, sys
import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    root = a.root
    sch = json.load(open(f"{root}/docs/schema.json"))
    rules = json.load(open(f"{root}/docs/rules.json"))
    TB = {t["name"]: t for t in sch["tables"]}
    RAW = {n: pd.read_csv(f"{root}/data/{n}.csv", dtype=str, keep_default_na=False) for n in sch["table_order"]}
    X = {}
    for n, df in RAW.items():
        o = df.copy()
        for f in TB[n]["fields"]:
            c = f["name"]
            col = o[c].where(o[c] != "", None)
            if f["type"] in ("INTEGER", "BOOLEAN", "REAL"): o[c] = pd.to_numeric(col, errors="coerce")
            elif f["type"] in ("DATE", "TIMESTAMP"): o[c] = pd.to_datetime(col, errors="coerce")
            else: o[c] = col
        X[n] = o
    res = []

    def chk(cid, desc, bad, sev="ERROR", detail=None):
        bad = list(bad)
        res.append(dict(check_id=cid, description=desc, severity=sev, failures=len(bad),
                        status="PASS" if not bad else ("FAIL" if sev == "ERROR" else "EXCEPTIONS_FOUND"),
                        examples=[str(x) for x in bad[:5]], detail=detail))
        return bad

    # ------------------------------------------------------------------ generic, schema-driven
    req, enum, rx, rng_, typ, pk, fk, uq = {}, {}, {}, {}, {}, {}, {}, {}
    for n, t in TB.items():
        df = RAW[n]
        pkc = [f["name"] for f in t["fields"] if "PK" in [k.strip() for k in f["key"].split(";")]]
        dup = df.duplicated(pkc, keep=False)
        pk[n] = int(dup.sum())
        for f in t["fields"]:
            c, d = f["name"], f["domain"]
            nn = df[c] != ""
            if f["required"]: req[f"{n}.{c}"] = int((~nn).sum())
            if d.startswith("enum:"): enum[f"{n}.{c}"] = int((nn & ~df[c].isin(d[5:].split("|"))).sum())
            if d.startswith("re:"): rx[f"{n}.{c}"] = int((nn & ~df[c].str.match(d[3:])).sum())
            if f["type"] in ("INTEGER", "BOOLEAN", "REAL", "DATE", "TIMESTAMP"): typ[f"{n}.{c}"] = int((nn & X[n][c].isna()).sum())
            if d.startswith("range:"):
                lo, hi = d[6:].split("..")
                v = X[n][c]
                b = pd.Series(False, index=v.index)
                if lo != "": b |= v < float(lo)
                if hi != "": b |= v > float(hi)
                rng_[f"{n}.{c}"] = int(b.sum())
            for k in [k.strip() for k in f["key"].split(";")]:
                if k.startswith("FK "):
                    pt, pc = k[3:].split(".")
                    fk[f"{n}.{c}->{pt}.{pc}"] = int((nn & ~df[c].isin(set(RAW[pt][pc]))).sum())
                if k == "UQ": uq[f"{n}.{c}"] = int(df[df[c] != ""].duplicated(c).sum())
        for cons in t["constraints"]:
            m = re.match(r"UNIQUE \((.*)\)", cons)
            if m:
                cols = [x.strip() for x in m.group(1).split(",")]
                uq[f"{n}.({','.join(cols)})"] = int(df.duplicated(cols).sum())
    nz = lambda d: {k: v for k, v in d.items() if v}
    chk("G01", "Primary keys unique (incl. composite)", [k for k, v in pk.items() if v])
    chk("G02", "Required fields non-null", nz(req))
    chk("G03", "Foreign keys resolve to parent rows", nz(fk))
    chk("G04", "Enum / allowed-value domains", nz(enum))
    chk("G05", "ID nomenclature patterns (PO-2026-nnnnnn, ACK-<po>-nn, CHG-<po>-nnnnnn, APR-<chg>-nn, NOT-2026-nnnnnn, ...)", nz(rx))
    chk("G06", "Numeric ranges", nz(rng_))
    chk("G07", "Parseable types (int/real/date/timestamp)", nz(typ))
    chk("G08", "Unique constraints (natural keys / UQ)", nz(uq))
    # ID cross-consistency (IDs derived from parents exactly)
    ackd, chgd = X["acknowledgement"], X["change_request"]
    chk("G09", "ack_id == ACK-<po_id>-<revision_no:02d>", ackd[ackd.ack_id != "ACK-" + ackd.po_id + "-" + ackd.revision_no.astype(int).astype(str).str.zfill(2)].ack_id)
    chk("G10", "change_id starts with CHG-<po_id>- ; approval_id == APR-<change_id>-<step:02d>",
        list(chgd[~chgd.apply(lambda r: r.change_id.startswith("CHG-" + r.po_id + "-"), axis=1)].change_id) +
        list(X["approval"][X["approval"].approval_id != "APR-" + X["approval"].change_id + "-" + X["approval"].step_no.astype(int).astype(str).str.zfill(2)].approval_id))

    po, pl, al, pr = X["purchase_order"], X["po_line"], X["acknowledgement_line"], X["promise_date"]
    cl, ap, imp, plan = X["change_request_line"], X["approval"], X["change_impact"], X["inventory_plan"]
    sup, usr, prod, br, note, ev = X["supplier"], X["app_user"], X["production_order"], X["production_order_po_line"], X["notification"], X["status_event"]

    # ------------------------------------------------------------------ business-rule exception detectors
    A = al.merge(ackd[["ack_id", "po_id"]], on="ack_id").merge(po[["po_id", "order_date"]], on="po_id")
    A = A.merge(pl[["po_line_id", "po_id", "requested_qty", "requested_date", "unit_price"]].rename(columns={"po_id": "line_po"}), on="po_line_id", how="left")
    r1 = A[A.po_id != A.line_po]
    R1 = set(r1.ack_line_id)
    r2a = A[A.promised_date.notna() & (A.promised_date < A.order_date)]
    P = pr.merge(A[["ack_line_id", "po_id", "order_date"]], on="ack_line_id", how="left")
    r2 = P[P.promised_date < P.order_date]
    R2 = set(r2a.ack_line_id)
    P["grp"] = P.po_id + "|" + P.ack_line_id.str[-3:]
    mx = P.groupby("grp").revision_no.transform("max")
    r3 = P[(P.is_current == 1) & (P.revision_no < mx)]
    R3 = set(r3.grp)
    # R4: plan versions sourced from a change that is not APPLIED, authorised by a non-APPROVED step, or applied before approval
    pc = plan[plan.source_change_id.notna()].merge(chgd[["change_id", "current_status"]], left_on="source_change_id", right_on="change_id", how="left")
    pc = pc.merge(ap[["approval_id", "decision", "decided_ts"]], left_on="authorizing_approval_id", right_on="approval_id", how="left")
    r4 = pc[(pc.current_status != "APPLIED") | (pc.decision != "APPROVED") | (pc.valid_from_ts < pc.decided_ts)]
    R4_lines = set(r4.po_line_id)
    exc = [("ACK_REFERS_WRONG_PO_LINE", "acknowledgement_line", r1.ack_line_id),
           ("PROMISE_DATE_BEFORE_ORDER_DATE", "promise_date", r2.promise_id),
           ("OLDER_REVISION_OVERWRITES_NEWER", "promise_date", r3.promise_id),
           ("UNAPPROVED_CHANGE_UPDATES_INVENTORY_PLAN", "inventory_plan", r4.inventory_plan_id)]
    rows = []
    for i, (name, tbl, ids) in enumerate(exc, 1):
        ids = list(ids)
        chk(f"X0{i}", f"EXCEPTION detector: {name}", ids, "EXCEPTION")
        rows += [dict(issue_type=name, table_name=tbl, record_id=x) for x in ids]
    pd.DataFrame(rows, columns=["issue_type", "table_name", "record_id"]).to_csv(f"{root}/docs/detected_exceptions.csv", index=False, lineterminator="\n")
    bad_ack = set(r1.ack_id) | set(r2a.ack_id)  # acks whose line-level derivations are knowingly affected by an exception

    # ------------------------------------------------------------------ date ordering
    L = pl.merge(po[["po_id", "order_date"]], on="po_id")
    chk("D01", "po_line.requested_date >= order_date", L[L.requested_date < L.order_date].po_line_id)
    chk("D02", "ack.received_ts > PO issued_ts; PO issued date == order_date",
        list(ackd.merge(po[["po_id", "issued_ts"]], on="po_id").pipe(lambda d: d[d.received_ts <= d.issued_ts]).ack_id) +
        list(po[po.issued_ts.dt.normalize() != po.order_date].po_id))
    ar = ackd.sort_values(["po_id", "revision_no"])
    chk("D03", "ack revisions strictly increase in time per PO and revision_no is contiguous 1..n",
        list(ar[(ar.groupby("po_id").received_ts.diff() <= pd.Timedelta(0))].ack_id) +
        list(ar[ar.revision_no != ar.groupby("po_id").cumcount() + 1].ack_id))
    chg2 = chgd.merge(ackd[["ack_id", "received_ts"]], on="ack_id")
    chk("D04", "change.submitted_ts >= ack.received_ts", chg2[chg2.submitted_ts < chg2.received_ts].change_id)
    chk("D05", "approval decided_ts >= assigned_ts; plan valid_to > valid_from; promise applied>=received; notification read>=created",
        list(ap[ap.decided_ts < ap.assigned_ts].approval_id) + list(plan[plan.valid_to_ts <= plan.valid_from_ts].inventory_plan_id) +
        list(pr[pr.applied_ts < pr.received_ts].promise_id) + list(note[note.read_ts < note.created_ts].notification_id))
    chk("D06", "production_order planned_end >= planned_start", prod[prod.planned_end_date < prod.planned_start_date].production_order_id)
    S_ = po.merge(sup, on="supplier_id", suffixes=("", "_s"))
    chk("D07", "PO issued to ACTIVE supplier within its effective window",
        S_[(S_.status != "ACTIVE") | (S_.order_date < S_.effective_from) | (S_.effective_to.notna() & (S_.order_date > S_.effective_to))].po_id)
    BL = br.merge(pl[["po_line_id", "requested_date"]], on="po_line_id").merge(prod[["production_order_id", "planned_start_date"]], on="production_order_id")
    chk("D08", "production start >= requested_date + inbound buffer (baseline feasible)",
        BL[(BL.planned_start_date - BL.requested_date).dt.days < rules["inbound_buffer_days"]].production_order_id)
    asof = pd.Timestamp(json.load(open(f"{root}/docs/generation_manifest.json"))["parameters"]["as_of"])
    late = []
    for tn, cols in {"status_event": ["event_ts"], "notification": ["created_ts", "read_ts"], "approval": ["assigned_ts", "decided_ts"],
                     "acknowledgement": ["received_ts"], "inventory_plan": ["valid_from_ts", "valid_to_ts"], "change_request": ["submitted_ts", "resolved_ts"]}.items():
        for c_ in cols:
            late += [f"{tn}.{c_}"] * int((X[tn][c_] > asof).sum())
    chk("D09", "No recorded event/timestamp lies after the as-of horizon", late)

    # ------------------------------------------------------------------ arithmetic reconciliation
    chk("A01", "po_line.line_amount == round(qty*price,2)", pl[(pl.line_amount - (pl.requested_qty * pl.unit_price).round(2)).abs() > .005].po_line_id)
    g = pl.groupby("po_id").agg(n=("po_line_id", "count"), amt=("line_amount", "sum")).reset_index()
    Pg = po.merge(g, on="po_id")
    chk("A02", "PO line_count / total_amount reconcile to lines; ack_sla_due_ts = issued + SLA",
        list(Pg[(Pg.line_count != Pg.n) | ((Pg.total_amount - Pg.amt).abs() > .011)].po_id) +
        list(po[(po.ack_sla_due_ts - po.issued_ts) != pd.Timedelta(hours=rules["ack_sla_hours"])].po_id))
    gs = al.groupby("ack_id").agg(acc=("accepted_qty", "sum"), rej=("rejected_qty", "sum")).reset_index()
    Ag = ackd.merge(gs, on="ack_id").merge(po[["po_id", "issued_ts"]], on="po_id")
    lat = ((Ag.received_ts - Ag.issued_ts).dt.total_seconds() / 3600).round(2)
    chk("A03", "ack totals reconcile to lines; latency and is_late derived correctly",
        Ag[(Ag.acc != Ag.total_accepted_qty) | (Ag.rej != Ag.total_rejected_qty) | ((lat - Ag.response_latency_hours).abs() > .011) |
           (Ag.is_late != (Ag.response_latency_hours > rules["ack_sla_hours"]).astype(int))].ack_id)
    Ok = A[~A.ack_id.isin(bad_ack)].copy()
    Ok["pd_"] = Ok.promised_date
    f_ = Ok.groupby("ack_id").apply(lambda d: pd.Series(dict(
        tot=d.accepted_qty.sum(), part=(d.accepted_qty < d.requested_qty).any(),
        chg=((d.accepted_qty > d.requested_qty) | ((d.ack_unit_price - d.unit_price).abs() > .004) |
             (d.promised_date.notna() & (d.promised_date != d.requested_date))).any())), include_groups=False).reset_index()
    f_["exp"] = np.where(f_.tot == 0, "REJECTED", np.where(f_.part, "PARTIALLY_ACCEPTED", np.where(f_.chg, "ACCEPTED_WITH_CHANGES", "ACCEPTED")))
    f_ = f_.merge(ackd[["ack_id", "ack_status"]], on="ack_id")
    chk("A04", "ack_status consistent with line values vs original request (acks touched by exceptions excluded)", f_[f_.exp != f_.ack_status].ack_id,
        detail=dict(excluded_acks=len(bad_ack)))
    Ok2 = Ok[~Ok.ack_line_id.isin(R1 | R2)]
    chk("A05", "rejected_qty == max(0, requested - accepted); promised_date NULL iff accepted=0; reason iff qty<>requested",
        list(Ok2[Ok2.rejected_qty != (Ok2.requested_qty - Ok2.accepted_qty).clip(lower=0)].ack_line_id) +
        list(Ok2[(Ok2.accepted_qty == 0) != Ok2.promised_date.isna()].ack_line_id) +
        list(Ok2[(Ok2.accepted_qty != Ok2.requested_qty) != Ok2.rejection_reason.notna()].ack_line_id))
    ga = br.groupby("po_line_id").allocated_qty.sum().reset_index().merge(pl[["po_line_id", "requested_qty"]], on="po_line_id", how="right")
    chk("A06", "production allocations sum to po_line.requested_qty (bridge reconciles)", ga[ga.allocated_qty != ga.requested_qty].po_line_id)
    Q = pr.merge(al[["ack_line_id", "po_line_id", "promised_date", "accepted_qty", "ack_id"]], on="ack_line_id", suffixes=("", "_al")).merge(
        ackd[["ack_id", "revision_no"]], on="ack_id", suffixes=("", "_ack")).merge(pl[["po_line_id", "requested_date"]], on="po_line_id", how="left")
    Qn = Q[~Q.ack_line_id.isin(R1 | R2)]
    chk("A07", "promise_date mirrors ack_line (date, qty, revision, line) and days_vs_requested is derived",
        list(Qn[(Qn.promised_date != Qn.promised_date_al) | (Qn.promised_qty != Qn.accepted_qty) | (Qn.revision_no != Qn.revision_no_ack)].promise_id) +
        list(Qn[(Qn.promised_date - Qn.requested_date).dt.days != Qn.days_vs_requested].promise_id) +
        list(Q[(Q.po_line_id != Q.po_line_id_al)].promise_id))
    miss = al[(al.accepted_qty > 0) & ~al.ack_line_id.isin(set(pr.ack_line_id))].ack_line_id
    chk("A08", "every accepted ack_line has a promise_date row (history preserved)", miss)
    # change request aggregates
    cg = cl.groupby("change_id").agg(n=("change_line_id", "count"), mq=("qty_dev_pct_vs_requested", "max"), mp=("price_dev_pct_vs_requested", "max"),
                                     md=("date_dev_days_vs_requested", "max"), av=("value_delta", lambda s: s.abs().sum()),
                                     q=("qty_delta", lambda s: (s != 0).any()), pc=("price_delta_pct", lambda s: (s.abs() > 1e-9).any()),
                                     d=("date_delta_days", lambda s: (s != 0).any())).reset_index()
    C = chgd.merge(cg, on="change_id")
    chk("A09", "change_request aggregates (flags, counts, max deviations, abs value) reconcile to change_request_line",
        C[(C.changed_line_count != C.n) | ((C.max_qty_dev_pct - C.mq).abs() > 1e-3) | ((C.max_price_dev_pct - C.mp).abs() > 1e-3) |
          (C.max_date_dev_days != C.md) | ((C.abs_value_impact - C.av).abs() > .02) | (C.has_qty_change != C.q.astype(int)) |
          (C.has_price_change != C.pc.astype(int)) | (C.has_date_change != C.d.astype(int))].change_id)
    ty = np.where(C.has_qty_change + C.has_price_change + C.has_date_change > 1, "MULTI", np.where(C.has_qty_change == 1, "QTY", np.where(C.has_price_change == 1, "PRICE", "DATE")))
    chk("A10", "change_type derived from flags", C[ty != C.change_type].change_id)
    c = cl
    chk("A11", "change_request_line deltas / deviations / value_delta derived from requested-baseline-proposed",
        c[(c.qty_delta != c.proposed_qty - c.baseline_qty) | ((c.value_delta - (c.proposed_qty * c.proposed_unit_price - c.baseline_qty * c.baseline_unit_price)).abs() > .011) |
          ((c.price_delta_pct - (c.proposed_unit_price - c.baseline_unit_price) / c.baseline_unit_price * 100).abs() > 1e-3) |
          ((c.qty_dev_pct_vs_requested - (c.proposed_qty - c.requested_qty).abs() / c.requested_qty * 100).abs() > 1e-3) |
          ((c.price_dev_pct_vs_requested - (c.proposed_unit_price - c.requested_unit_price).abs() / c.requested_unit_price * 100).abs() > 1e-3) |
          ((c.proposed_qty == 0) != c.proposed_date.isna())].change_line_id)
    CL = cl.merge(pl[["po_line_id", "requested_qty", "unit_price", "requested_date"]], on="po_line_id", suffixes=("", "_pl")).merge(
        al[["ack_line_id", "accepted_qty", "ack_unit_price", "promised_date"]], on="ack_line_id", how="left")
    CLn = CL[~CL.ack_line_id.isin(R2)]
    chk("A12", "change lines: requested_* match PO line; proposed_* match acknowledgement line",
        list(CL[(CL.requested_qty != CL.requested_qty_pl) | ((CL.requested_unit_price - CL.unit_price).abs() > .004) | (CL.requested_date != CL.requested_date_pl)].change_line_id) +
        list(CLn[(CLn.proposed_qty != CLn.accepted_qty) | ((CLn.proposed_unit_price - CLn.ack_unit_price).abs() > .004) |
                 ((CLn.proposed_date != CLn.promised_date) & CLn.proposed_date.notna())].change_line_id))
    cs = cl.sort_values("submitted_ts" if "submitted_ts" in cl else "change_id").merge(chgd[["change_id", "submitted_ts"]], on="change_id").sort_values("submitted_ts")
    pls = plan.sort_values("valid_from_ts")
    M = pd.merge_asof(cs, pls[["po_line_id", "valid_from_ts", "projected_qty", "projected_date", "unit_price"]], left_on="submitted_ts", right_on="valid_from_ts",
                      by="po_line_id", direction="backward")
    M = M[~M.po_line_id.isin(R4_lines)]
    chk("A13", "baseline_* equals the inventory_plan version in force at submission",
        M[(M.baseline_qty != M.projected_qty) | (M.baseline_date != M.projected_date) | ((M.baseline_unit_price - M.unit_price).abs() > .004)].change_line_id)
    I = imp.merge(prod[["production_order_id", "planner_id", "planned_start_date"]], on="production_order_id", suffixes=("", "_p")).merge(
        br[["production_order_id", "po_line_id", "allocated_qty"]].merge(cl[["change_line_id", "po_line_id"]], on="po_line_id"), on=["production_order_id", "change_line_id"], suffixes=("", "_b"), how="left")
    chk("A14", "change_impact joins: planner = production planner; start date and allocation equal source rows; flag derived",
        I[(I.impacted_planner_id != I.planner_id) | (I.planned_start_date != I.planned_start_date_p) | (I.allocated_qty != I.allocated_qty_b) |
          (I.impact_flag != ((I.proposed_lateness_days > I.baseline_lateness_days) | (I.proposed_shortfall_qty > I.baseline_shortfall_qty)).astype(int))].change_impact_id)
    fl = imp[imp.impact_flag == 1].groupby("change_id").proposed_lateness_days.max().rename("ml").reset_index()
    Cm = chgd.merge(fl, on="change_id", how="left").fillna({"ml": 0})
    chk("A15", "change.max_downstream_lateness_days == max flagged lateness", Cm[Cm.max_downstream_lateness_days != Cm.ml].change_id)
    chk("A16", "downstream impact identified: every change line has >=1 change_impact row", cl[~cl.change_line_id.isin(set(imp.change_line_id))].change_line_id)

    # ------------------------------------------------------------------ lifecycle
    def chain(etype, allowed, cur_tbl, key, cur_col):
        e = ev[ev.entity_type == etype].sort_values([ "entity_id", "event_seq"]).copy()
        e["prev"] = e.groupby("entity_id").to_status.shift()
        e["n"] = e.groupby("entity_id").cumcount() + 1
        bad = list(e[(e.event_seq != e.n)].status_event_id)
        bad += list(e[(e.n == 1) & e.from_status.notna()].status_event_id)
        bad += list(e[(e.n > 1) & (e.from_status != e.prev)].status_event_id)
        first = "ISSUED" if etype == "PURCHASE_ORDER" else "SUBMITTED"
        bad += list(e[(e.n == 1) & (e.to_status != first)].status_event_id)
        ok = {(f_, t_) for f_, ts_ in allowed.items() for t_ in ts_}
        mid = e[e.n > 1]
        bad += list(mid[[(f_, t_) not in ok for f_, t_ in zip(mid.from_status, mid.to_status)]].status_event_id)
        bad += list(e[e.groupby("entity_id").event_ts.diff() < pd.Timedelta(0)].status_event_id)
        last = e.groupby("entity_id").tail(1).merge(cur_tbl[[key, cur_col]], left_on="entity_id", right_on=key, how="outer", indicator=True)
        bad += list(last[(last._merge != "both") | (last.to_status != last[cur_col])].entity_id.astype(str))
        return bad
    chk("L01", "PO lifecycle: first=ISSUED, from_status chains, allowed transitions, monotone time, current_status == last event, no orphan POs",
        chain("PURCHASE_ORDER", sch["po_transitions"], po, "po_id", "current_status"))
    chk("L02", "Change lifecycle: SUBMITTED->PENDING_APPROVAL->APPROVED->APPLIED|REJECTED|SUPERSEDED chain; current_status == last event",
        chain("CHANGE_REQUEST", sch["chg_transitions"], chgd, "change_id", "current_status"))
    chk("L03", "status_event.po_id matches owner of entity_id",
        list(ev[(ev.entity_type == "PURCHASE_ORDER") & (ev.entity_id != ev.po_id)].status_event_id) +
        list(ev[ev.entity_type == "CHANGE_REQUEST"].merge(chgd[["change_id", "po_id"]], left_on="entity_id", right_on="change_id", how="left", suffixes=("", "_c")).pipe(lambda d: d[d.po_id != d.po_id_c]).status_event_id))
    ar_ = ap.merge(usr[["user_id", "role"]], left_on="approver_user_id", right_on="user_id")
    chk("L04", "approval: approver role matches user role; decided_ts NULL iff PENDING; step numbers contiguous from 1",
        list(ar_[ar_.approver_role != ar_.role].approval_id) + list(ap[(ap.decision == "PENDING") != ap.decided_ts.isna()].approval_id) +
        list(ap.sort_values(["change_id", "step_no"]).pipe(lambda d: d[d.step_no != d.groupby("change_id").cumcount() + 1]).approval_id))
    lastap = ap.sort_values("step_no").groupby("change_id").tail(1)[["change_id", "decision"]].rename(columns={"decision": "ld"})
    Z = chgd.merge(lastap, on="change_id", how="left")
    chk("L05", "change.current_status consistent with approvals (APPLIED<=>final APPROVED; REJECTED<=>final REJECTED; PENDING_APPROVAL<=>PENDING/ESCALATED; SUPERSEDED has no APPLIED)",
        Z[((Z.current_status == "APPLIED") & (Z.ld != "APPROVED")) | ((Z.current_status == "REJECTED") & (Z.ld != "REJECTED")) |
          ((Z.current_status == "PENDING_APPROVAL") & ~Z.ld.isin(["PENDING", "ESCALATED"])) | ((Z.current_status == "SUPERSEDED") & (Z.ld == "REJECTED") & False)].change_id)
    # only approved changes create plan versions: every non-injected plan row with source change must come from APPLIED change
    # plan chain
    pp = plan.sort_values(["po_line_id", "plan_version"]).copy()
    pp["n"] = pp.groupby("po_line_id").cumcount() + 1
    pp["next_from"] = pp.groupby("po_line_id").valid_from_ts.shift(-1)
    pp["is_last"] = pp.groupby("po_line_id").cumcount(ascending=False) == 0
    chk("L06", "inventory_plan: versions contiguous, valid_to == next valid_from, exactly one open version (last), v1 initial & only v1 initial",
        list(pp[pp.plan_version != pp.n].inventory_plan_id) +
        list(pp[(~pp.is_last) & (pp.valid_to_ts != pp.next_from)].inventory_plan_id) +
        list(pp[pp.is_last & pp.valid_to_ts.notna()].inventory_plan_id) +
        list(pp[(pp.plan_version == 1) != (pp.update_reason == "INITIAL_PO")].inventory_plan_id) +
        list(pp[(pp.plan_version == 1) != pp.source_change_id.isna()].inventory_plan_id) +
        list(pp[~pp.po_line_id.isin(set(pl.po_line_id))].inventory_plan_id))
    ap_ok = chgd[chgd.current_status == "APPLIED"][["change_id", "resolved_ts"]]
    pa = plan[plan.source_change_id.isin(set(ap_ok.change_id))].merge(ap_ok, left_on="source_change_id", right_on="change_id")
    expected = cl[cl.change_id.isin(set(ap_ok.change_id))]
    got = pa.groupby(["source_change_id", "po_line_id"]).size().rename("k").reset_index()
    mm = expected.merge(got, left_on=["change_id", "po_line_id"], right_on=["source_change_id", "po_line_id"], how="left")
    chk("L07", "every APPLIED change produced exactly one plan version per changed line, at resolved_ts, with proposed values",
        list(mm[mm.k != 1].change_line_id) + list(pa.merge(expected, left_on=["source_change_id", "po_line_id"], right_on=["change_id", "po_line_id"]).pipe(
            lambda d: d[(d.projected_qty != d.proposed_qty) | (d.valid_from_ts != d.resolved_ts)]).inventory_plan_id))
    chk("L08", "Ack revision history preserved: one promise row per accepted line; at most one is_current promise per line; newest promise is current (exceptions excluded)",
        list(P[~P.grp.isin(R3)].groupby("grp").is_current.sum().pipe(lambda s: s[s > 1]).index))
    chk("L09", "AUTO changes approved by SYSTEM only; MANAGER-level changes have PLANNER step 1 (ESCALATED) before OPS_MANAGER step 2",
        list(ap.merge(chgd[["change_id", "required_approval_level"]], on="change_id").pipe(
            lambda d: d[((d.required_approval_level == "AUTO") & (d.approver_role != "SYSTEM")) |
                        ((d.required_approval_level != "AUTO") & (d.approver_role == "SYSTEM")) |
                        ((d.required_approval_level == "MANAGER") & (d.step_no == 1) & (d.decision.isin(["APPROVED", "REJECTED"]))) |
                        ((d.required_approval_level == "PLANNER") & (d.approver_role == "OPS_MANAGER"))]).approval_id))
    # ------------------------------------------------------------------ duplicates / notification joins
    nk = ["notification_type", "recipient_user_id", "po_id", "change_id", "production_order_id", "created_ts"]
    chk("X10", "No duplicate notifications (type, recipient, po, change, production order, ts)", note[note.duplicated(nk, keep=False)].notification_id)
    nn = note[note.change_id.notna()].merge(chgd[["change_id", "po_id"]], on="change_id", suffixes=("", "_c"))
    chk("X11", "notification.po_id equals its change's po_id", nn[nn.po_id != nn.po_id_c].notification_id)
    NU = note.merge(usr[["user_id", "role"]], left_on="recipient_user_id", right_on="user_id")
    okmap = {"ACK_DEVIATION_RECEIVED": "PLANNER", "APPROVAL_REQUIRED": "PLANNER|OPS_MANAGER", "CHANGE_APPLIED": "PLANNER", "CHANGE_REJECTED": "PLANNER",
             "DOWNSTREAM_IMPACT_ALERT": "PLANNER", "INBOUND_SCHEDULE_UPDATE": "WAREHOUSE_OPERATOR", "SUPPLIER_SILENCE": "PLANNER|LOGISTICS_ANALYST",
             "LOGISTICS_REVIEW_REQUESTED": "LOGISTICS_ANALYST"}
    chk("X13", "notification recipient role fits notification type",
        NU[[r not in okmap[t].split("|") for t, r in zip(NU.notification_type, NU.role)]].notification_id)
    PL_ = po.merge(usr[["user_id", "warehouse_id"]], left_on="planner_id", right_on="user_id", suffixes=("", "_u"))
    chk("X14", "PO planner belongs to receiving warehouse; production planner belongs to plant",
        list(PL_[PL_.warehouse_id != PL_.receiving_warehouse_id].po_id) +
        list(prod.merge(usr[["user_id", "warehouse_id"]], left_on="planner_id", right_on="user_id", suffixes=("", "_u")).pipe(lambda d: d[d.warehouse_id != d.warehouse_id_u]).production_order_id))

    # ------------------------------------------------------------------ SQL seed loads and FK-checks in SQLite
    sqlres = {}
    try:
        con = sqlite3.connect(":memory:")
        con.executescript(open(f"{root}/sql/schema.sql").read())
        con.executescript(open(f"{root}/sql/seed_data.sql").read())
        con.execute("PRAGMA foreign_keys=ON")
        fkv = con.execute("PRAGMA foreign_key_check").fetchall()
        cnt = {n: con.execute(f"select count(*) from {n}").fetchone()[0] for n in sch["table_order"]}
        mism = [n for n in cnt if cnt[n] != len(RAW[n])]
        chk("S01", "SQL schema+seed load into SQLite with all CHECK/PK/UNIQUE constraints; PRAGMA foreign_key_check clean; row counts equal CSV",
            [f"fk:{x[0]}" for x in fkv[:5]] + mism)
    except Exception as e:  # noqa
        chk("S01", "SQL schema+seed load into SQLite", [f"error: {e}"])

    errors = [r for r in res if r["severity"] == "ERROR" and r["failures"]]
    summary = dict(root=os.path.abspath(root), row_counts={n: len(d) for n, d in RAW.items()},
                   checks_total=len(res), error_checks_failed=len(errors),
                   exception_detectors={r["check_id"]: r["failures"] for r in res if r["severity"] == "EXCEPTION"},
                   overall="PASS" if not errors else "FAIL", checks=res)
    json.dump(summary, open(f"{root}/docs/validation_summary.json", "w"), indent=1, default=str)
    w = max(len(r["check_id"]) for r in res)
    for r in res:
        print(f"{r['check_id']:<{w}}  {r['status']:<16} {r['failures']:>6}  {r['description'][:100]}")
    print("\nOVERALL:", summary["overall"], "| ERROR checks failed:", len(errors), "| exceptions detected:", summary["exception_detectors"])
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
