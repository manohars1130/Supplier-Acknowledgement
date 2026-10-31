#!/usr/bin/env python3
"""YottaFlex - supplier acknowledgement / promise-date / change-approval synthetic data generator.

Same generator for starter and full datasets: only --n-pos / --n-events change.
Pure standard library, single seeded RNG, no wall-clock or hash-order dependence => byte-reproducible.

    python3 generate_data.py --seed 20261022 --n-pos 10000 --n-events 15000 --out .
"""
import argparse, csv, hashlib, json, math, os, random, sys
from collections import defaultdict
from datetime import datetime, date, timedelta

import schema_def as S

VERSION = "1.0.0"
TIERS = {  # per-line propensities and hard-silence probability by supplier reliability tier
    "A": dict(p_date=.07, p_qty=.03, p_price=.025, silence=.02),
    "B": dict(p_date=.15, p_qty=.08, p_price=.06, silence=.06),
    "C": dict(p_date=.28, p_qty=.16, p_price=.10, silence=.12),
}
REASONS = ["CAPACITY", "MATERIAL_SHORTAGE", "MOQ", "DISCONTINUED"]
MIN = timedelta(minutes=1)
R = S.RULES
SLA_H = R["ack_sla_hours"]
BUF = timedelta(days=R["inbound_buffer_days"])


# ----------------------------------------------------------------------------- helpers
def ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def bd(d, step=1):
    while d.weekday() >= 5:
        d += timedelta(days=step)
    return d


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def po_id_of(n): return f"PO-2026-{n:06d}"


# ----------------------------------------------------------------------------- simulation state
class PO:
    def __init__(s, po, sup, lines):
        s.po, s.sup, s.lines = po, sup, lines
        s.acks, s.chgs, s.chg_seq, s.hard_silent = [], [], 0, False
        s.plan = {i: [dict(vf=po["issued"], qty=l["requested_qty"], price=l["unit_price"],
                           date=l["requested_date"], chg=None, appr=None)] for i, l in enumerate(lines)}

    def plan_at(s, li, t):
        cur = s.plan[li][0]
        for v in s.plan[li]:
            if v["vf"] <= t:
                cur = v
        return cur


def level_for(max_date, max_qty, max_price, abs_val, flagged, lateness, shortfall):
    th, dn = R["approval_thresholds"], R["downstream_escalation"]
    lvl, codes = 0, []

    def up(l, c):
        nonlocal lvl
        lvl = max(lvl, l)
        codes.append(c)
    if max_date > th["date_days_abs"]["planner_max"]: up(2, "DATE_GT_10D")
    elif max_date > th["date_days_abs"]["auto_max"]: up(1, "DATE_3_10D")
    if max_qty > th["qty_pct_abs"]["planner_max"]: up(2, "QTY_GT_40PCT")
    elif max_qty > th["qty_pct_abs"]["auto_max"]: up(1, "QTY_10_40PCT")
    if max_price > th["price_pct_abs"]["planner_max"]: up(2, "PRICE_GT_5PCT")
    elif max_price > th["price_pct_abs"]["auto_max"]: up(1, "PRICE_1_5PCT")
    if abs_val >= th["abs_value_impact_usd"]["manager_min"]: up(2, "VALUE_GE_25K")
    elif abs_val >= th["abs_value_impact_usd"]["planner_min"]: up(1, "VALUE_GE_5K")
    if flagged:
        up(1, "DOWNSTREAM_IMPACT")
        if lateness > dn["manager_if_lateness_days_gt"] or shortfall >= dn["manager_if_shortfall_ratio_ge"]:
            up(2, "DOWNSTREAM_CRITICAL")
    return ("AUTO", "PLANNER", "MANAGER")[lvl], codes or ["WITHIN_TOLERANCE"]


def decide(level, declined, price_dev, lateness, shortfall):
    d = R["decision_rules"]
    if level == "AUTO":
        return [("SYSTEM", "APPROVED", "AUTO_WITHIN_TOLERANCE")]
    if level == "PLANNER":
        if lateness > d["planner_reject_if_lateness_gt"]:
            return [("PLANNER", "REJECTED", "DOWNSTREAM_LATE_GT_7D")]
        if shortfall >= d["planner_reject_if_shortfall_ratio_ge"]:
            return [("PLANNER", "REJECTED", "DOWNSTREAM_SHORTFALL_GE_25PCT")]
        return [("PLANNER", "APPROVED", "WITHIN_PLANNER_AUTHORITY")]
    steps = [("PLANNER", "ESCALATED", "ESCALATE_TO_MANAGER")]
    if declined: steps.append(("OPS_MANAGER", "APPROVED", "ACCEPT_SUPPLIER_DECLINE"))
    elif price_dev > d["manager_reject_if_price_dev_gt"]: steps.append(("OPS_MANAGER", "REJECTED", "PRICE_GT_10PCT"))
    elif lateness > d["manager_reject_if_lateness_gt"]: steps.append(("OPS_MANAGER", "REJECTED", "DOWNSTREAM_LATE_GT_14D"))
    else: steps.append(("OPS_MANAGER", "APPROVED", "MANAGER_APPROVED"))
    return steps


def ack_status_of(vals, lines):
    if sum(v["qty"] for v in vals) == 0: return "REJECTED"
    if any(v["qty"] < l["requested_qty"] for v, l in zip(vals, lines)): return "PARTIALLY_ACCEPTED"
    if any(v["qty"] > l["requested_qty"] or abs(v["price"] - l["unit_price"]) > 0.004 or v["date"] != l["requested_date"]
           for v, l in zip(vals, lines)): return "ACCEPTED_WITH_CHANGES"
    return "ACCEPTED"


# ----------------------------------------------------------------------------- generator
def generate(a):
    rng = random.Random(a.seed)
    as_of = datetime.fromisoformat(a.as_of)
    as_of_d = as_of.date()
    T = {n: [] for n in S.TABLE_ORDER}
    GT_LABELS, ANOM = [], []

    # ---- master data -------------------------------------------------------------------
    WH = [("WH-01", "Rotterdam Cross-Dock", "EMEA", "2018-03-01"), ("WH-02", "Memphis Distribution Hub", "NA", "2017-06-15"),
          ("WH-03", "Singapore Regional DC", "APAC", "2019-01-10"), ("WH-04", "Pune Fulfilment Centre", "APAC", "2020-08-01"),
          ("WH-05", "Dallas Plant Warehouse", "NA", "2016-11-20"), ("WH-06", "Gdansk Assembly Warehouse", "EMEA", "2021-02-01")]
    for w in WH:
        T["warehouse"].append(dict(warehouse_id=w[0], warehouse_name=w[1], region=w[2], effective_from=w[3]))
    FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Ivy", "Kenji", "Amara", "Lucas", "Priya", "Omar", "Elena", "Mateo", "Hana",
             "Ravi", "Zoe", "Jonas", "Leila", "Tomas", "Anika", "Diego", "Mei", "Felix", "Nadia", "Arjun", "Clara", "Ethan", "Yara"]
    LAST = ["Nair", "Kowalski", "Okafor", "Tanaka", "Silva", "Meyer", "Chen", "Haddad", "Rossi", "Singh", "Larsen", "Costa",
            "Novak", "Reddy", "Alvarez", "Ito", "Brandt", "Mensah", "Petrov", "Walsh", "Iyer", "Fischer", "Moreau", "Khan"]
    users, uid = [], [0]

    def add_user(role, wh):
        uid[0] += 1
        u = dict(user_id=f"USR-{uid[0]:03d}", full_name=f"{rng.choice(FIRST)} {rng.choice(LAST)}" if role != "SYSTEM" else "System Auto-Approver",
                 role=role, warehouse_id=wh, is_active=1)
        users.append(u)
        return u["user_id"]
    SYS = add_user("SYSTEM", None)
    planners, prod_planners, managers, analysts, operators = {}, {}, {}, {}, {}
    for w in WH:
        planners[w[0]] = [add_user("PLANNER", w[0]) for _ in range(5)]
        managers[w[0]] = [add_user("OPS_MANAGER", w[0])]
        operators[w[0]] = [add_user("WAREHOUSE_OPERATOR", w[0]) for _ in range(2)]
        analysts[w[0]] = [add_user("LOGISTICS_ANALYST", w[0])]
    T["app_user"] = users
    role_of = {u["user_id"]: u["role"] for u in users}

    n_sku = max(60, a.n_pos // 12)
    CATS = [("ELECTRONICS", "EA", 3.4), ("MECHANICAL", "EA", 3.0), ("PACKAGING", "EA", 1.2), ("RAW_MATERIAL", "KG", 2.2), ("CHEMICAL", "L", 2.6)]
    skus = []
    for i in range(1, n_sku + 1):
        cat, uom, mu = rng.choice(CATS)
        skus.append(dict(sku_id=f"SKU-{i:04d}", description=f"{cat.title().replace('_', ' ')} item {i:04d}", category=cat, uom=uom,
                         standard_unit_price=round(clamp(math.exp(rng.gauss(mu, .9)), .5, 2500), 2)))
    T["sku"] = skus
    sku_by = {s["sku_id"]: s for s in skus}

    n_sup = max(15, round(a.n_pos / 50))
    P1 = ["Aurex", "Brightline", "Corvane", "Deltaforge", "Everton", "Fairmont", "Granite", "Helios", "Ironbridge", "Juniper", "Kestrel",
          "Lumina", "Meridian", "Northgate", "Orbis", "Pinecrest", "Quantum", "Redwood", "Solano", "Titan"]
    P2 = ["Components", "Industrial", "Materials", "Precision", "Polymers", "Metals", "Packaging", "Electronics", "Dynamics", "Supply",
          "Fabrication", "Chemicals", "Systems", "Alloys", "Plastics"]
    P3 = ["Ltd", "GmbH", "Inc", "Co", "SA", "Pte Ltd"]
    CTRY = ["Germany", "China", "India", "Vietnam", "Poland", "Mexico", "USA", "Japan", "Turkey", "Malaysia", "Italy", "Brazil"]
    combos = rng.sample(range(len(P1) * len(P2) * len(P3)), n_sup)
    suppliers = []
    win_start = as_of_d - timedelta(days=66)
    for i, cb in enumerate(combos, 1):
        tier = rng.choices(["A", "B", "C"], [30, 45, 25])[0]
        st = rng.choices(["ACTIVE", "ON_HOLD", "INACTIVE"], [92, 4, 4])[0]
        eff_from = date(2015, 1, 1) + timedelta(days=rng.randint(0, 3000))
        eff_to = (win_start - timedelta(days=rng.randint(5, 60))) if st == "INACTIVE" else None
        suppliers.append(dict(supplier_id=f"SUP-{i:04d}",
                              supplier_name=f"{P1[cb % len(P1)]} {P2[(cb // len(P1)) % len(P2)]} {P3[cb // (len(P1) * len(P2))]}",
                              country=rng.choice(CTRY), reliability_tier=tier, avg_lead_time_days=int(clamp(rng.gauss(28, 11), 7, 60)),
                              ack_sla_hours=SLA_H, status=st, effective_from=eff_from.isoformat(),
                              effective_to=eff_to.isoformat() if eff_to else None,
                              skus=rng.sample([s["sku_id"] for s in skus], min(len(skus), rng.randint(15, 40)))))
    T["supplier"] = [{k: v for k, v in s.items() if k != "skus"} for s in suppliers]
    active = [s for s in suppliers if s["status"] == "ACTIVE"]
    cumw, acc = [], 0.0
    for i in range(len(active)):
        acc += 1.0 / (i + 1) ** 0.5
        cumw.append(acc)

    # ---- purchase orders + lines + production demand ------------------------------------
    days = []
    d = win_start
    while d <= as_of_d - timedelta(days=2):
        if d.weekday() < 5: days.append(d)
        d += timedelta(days=1)
    drafts = [(rng.choice(days), rng.randint(7 * 3600, 18 * 3600)) for _ in range(a.n_pos)]
    drafts.sort()
    prod_slot, prods, bridge = defaultdict(list), [], []
    POS = []
    for n, (od, sec) in enumerate(drafts, 1):
        sup = rng.choices(active, cum_weights=cumw)[0]
        wh = rng.choice(WH)[0]
        pid = po_id_of(n)
        po = dict(po_id=pid, supplier_id=sup["supplier_id"], planner_id=rng.choice(planners[wh]), receiving_warehouse_id=wh,
                  order_date=od, issued=datetime(od.year, od.month, od.day) + timedelta(seconds=sec))
        nl = rng.choices([1, 2, 3, 4], [35, 30, 20, 15])[0]
        lines = []
        for ln, sk in enumerate(rng.sample(sup["skus"], nl), 1):
            q = max(10, int(round(math.exp(rng.gauss(4.6, 1.0)) / 5) * 5))
            q = min(q, 8000)
            price = round(sku_by[sk]["standard_unit_price"] * rng.uniform(.92, 1.08), 2)
            rd = bd(od + timedelta(days=max(5, int(round(rng.gauss(sup["avg_lead_time_days"], 4))))))
            line = dict(po_line_id=f"{pid}-L{ln:02d}", po_id=pid, line_no=ln, sku_id=sk, requested_qty=q, unit_price=price,
                        line_amount=round(q * price, 2), requested_date=rd, allocs=[])
            k = rng.choices([1, 2, 3], [60, 30, 10])[0]
            cuts = sorted(rng.sample(range(1, q), k - 1)) if k > 1 else []
            parts = [b - a_ for a_, b in zip([0] + cuts, cuts + [q])]
            used = []
            for part in parts:
                u = rng.random()
                slack = 0 if u < .06 else rng.randint(1, 3) if u < .20 else rng.randint(4, 7) if u < .45 else rng.randint(8, 14) if u < .75 else rng.randint(15, 28)
                pi = None
                if rng.random() < .4:
                    cands = []
                    for off in range(0, 17):
                        for c in prod_slot.get((wh, (rd + timedelta(days=2 + off)).toordinal()), []):
                            if c not in used: cands.append(c)
                    if cands: pi = rng.choice(cands)
                if pi is None:
                    start = bd(rd + timedelta(days=2 + slack))
                    prods.append(dict(production_order_id=f"PRD-2026-{len(prods) + 1:06d}", warehouse_id=wh,
                                      planner_id=rng.choice(planners[wh]), product_code=f"FG-{rng.randint(1000, 9999)}",
                                      planned_qty=rng.randint(20, 2000), planned_start_date=start,
                                      planned_end_date=start + timedelta(days=rng.randint(1, 6)),
                                      status="RELEASED" if start <= as_of_d else "PLANNED"))
                    pi = len(prods) - 1
                    prod_slot[(wh, start.toordinal())].append(pi)
                used.append(pi)
                line["allocs"].append((pi, part))
                bridge.append(dict(production_order_id=prods[pi]["production_order_id"], po_line_id=line["po_line_id"], allocated_qty=part))
            line["allocs"].sort(key=lambda x: (prods[x[0]]["planned_start_date"], x[0]))
            lines.append(line)
        ps = PO(po, sup, lines)
        POS.append(ps)
    T["production_order"] = [dict(p) for p in prods]
    for p in T["production_order"]:
        p["planned_start_date"] = p["planned_start_date"].isoformat()
        p["planned_end_date"] = p["planned_end_date"].isoformat()
    T["production_order_po_line"] = bridge

    # ---- simulation helpers -------------------------------------------------------------
    counters = dict(acks=0, chgs=0)

    def first_vals(ps):
        tr = TIERS[ps.sup["reliability_tier"]]
        vals = []
        for ln in ps.lines:
            q, p, dt_, reason = ln["requested_qty"], ln["unit_price"], ln["requested_date"], None
            if rng.random() < tr["p_qty"]:
                r = rng.random()
                if r < .12: q = 0
                elif r < .20: q = int(q * rng.uniform(1.02, 1.15)) + 1
                else:
                    q2 = int(q * (1 - (.02 + .7 * rng.random() ** 1.6)))
                    q = max(1, min(q2, q - 1))
            if rng.random() < tr["p_price"]:
                if rng.random() < .75:
                    f = 1.005 + .11 * rng.random() ** 1.5 + (.12 * rng.random() if rng.random() < .12 else 0)
                else:
                    f = 1 - (.01 + .04 * rng.random())
                p2 = round(p * f, 2)
                p = p2 if p2 != p else round(p + .01, 2)
            if rng.random() < tr["p_date"]:
                if rng.random() < .9:
                    dt_ = bd(dt_ + timedelta(days=int(clamp(math.exp(rng.gauss(1.5, .7)), 1, 45))))
                else:
                    cand = bd(dt_ - timedelta(days=rng.randint(1, 5)), -1)
                    if cand >= ps.po["order_date"] + timedelta(days=3): dt_ = cand
            vals.append(dict(qty=q, price=p, date=dt_ if q > 0 else None, reason=None))
        return vals

    def make_ack(ps, t, vals):
        rev = len(ps.acks) + 1
        for v, ln in zip(vals, ps.lines):
            if v["qty"] < ln["requested_qty"]: v["reason"] = v["reason"] or rng.choices(REASONS, [35, 35, 5, 25])[0]
            elif v["qty"] > ln["requested_qty"]: v["reason"] = "MOQ"
            else: v["reason"] = None
            if v["qty"] == 0: v["date"] = None
        ack = dict(ack_id=f"ACK-{ps.po['po_id']}-{rev:02d}", rev=rev, ts=t, vals=vals, channel=rng.choices(["EDI", "PORTAL", "EMAIL"], [50, 35, 15])[0],
                   delay=rng.randint(20, 240), chg=None)
        ps.acks.append(ack)
        counters["acks"] += 1
        return ack

    def supersede_open(ps, t):
        for ch in ps.chgs:
            if ch["superseded"] is None and t < ch["res_ts"]:
                new = []
                for st in ch["steps"]:
                    if st["decided"] <= t: new.append(st)
                    elif st["assigned"] < t:
                        new.append(dict(st, decision="WITHDRAWN", decided=t, reason="SUPERSEDED_BY_NEW_REVISION"))
                ch["steps"], ch["superseded"], ch["applied"] = new, t, None
                for li in ps.plan: ps.plan[li] = [v for v in ps.plan[li] if v["chg"] != ch["id"]]

    def cover(allocs, qty):
        rem, out = qty, []
        for pi, al in allocs:
            c = min(al, rem)
            rem -= c
            out.append((pi, al, c, al - c))
        return out

    def lateness(dt_, covq, start):
        return max(0, (dt_ + BUF - start).days) if (covq > 0 and dt_) else 0

    def make_change(ps, ack, t):
        lines_out, impacts = [], []
        ps.chg_seq += 1
        cid = f"CHG-{ps.po['po_id']}-{ps.chg_seq:06d}"
        for li, ln in enumerate(ps.lines):
            v, b = ack["vals"][li], ps.plan_at(li, t)
            pq, pp, pd_ = v["qty"], v["price"], v["date"]
            qd, prd, dd = pq != b["qty"], abs(pp - b["price"]) > .004, (pd_ is not None and pd_ != b["date"])
            if not (qd or prd or dd): continue
            lid = f"{cid}-L{ln['line_no']:02d}"
            row = dict(change_line_id=lid, change_id=cid, po_line_id=ln["po_line_id"], ack_line_id=f"{ack['ack_id']}-L{ln['line_no']:02d}",
                       requested_qty=ln["requested_qty"], requested_unit_price=ln["unit_price"], requested_date=ln["requested_date"],
                       baseline_qty=b["qty"], baseline_unit_price=b["price"], baseline_date=b["date"],
                       proposed_qty=pq, proposed_unit_price=pp, proposed_date=pd_, qty_delta=pq - b["qty"],
                       price_delta_pct=round((pp - b["price"]) / b["price"] * 100, 4),
                       date_delta_days=(pd_ - b["date"]).days if pd_ else 0,
                       qty_dev_pct_vs_requested=round(abs(pq - ln["requested_qty"]) / ln["requested_qty"] * 100, 4),
                       price_dev_pct_vs_requested=round(abs(pp - ln["unit_price"]) / ln["unit_price"] * 100, 4),
                       date_dev_days_vs_requested=abs((pd_ - ln["requested_date"]).days) if pd_ else 0,
                       value_delta=round(pq * pp - b["qty"] * b["price"], 2), _qd=qd, _prd=prd, _dd=dd)
            lines_out.append(row)
            cb, cp = cover(ln["allocs"], b["qty"]), cover(ln["allocs"], pq)
            for k, ((pi, al, c0, s0), (_, _, c1, s1)) in enumerate(zip(cb, cp), 1):
                st = prods[pi]["planned_start_date"]
                l0, l1 = lateness(b["date"], c0, st), lateness(pd_, c1, st)
                impacts.append(dict(change_impact_id=f"IMP-{lid}-{k:02d}", change_id=cid, change_line_id=lid,
                                    production_order_id=prods[pi]["production_order_id"], impacted_planner_id=prods[pi]["planner_id"],
                                    planned_start_date=st.isoformat(), allocated_qty=al, baseline_lateness_days=l0,
                                    proposed_lateness_days=l1, baseline_shortfall_qty=s0, proposed_shortfall_qty=s1,
                                    impact_flag=int(l1 > l0 or s1 > s0), _line=ln["requested_qty"]))
        if not lines_out: ps.chg_seq -= 1; return None
        flagged = [i for i in impacts if i["impact_flag"]]
        max_late = max([i["proposed_lateness_days"] for i in flagged], default=0)
        short_by_line = defaultdict(int)
        for i in flagged: short_by_line[i["change_line_id"]] += max(0, i["proposed_shortfall_qty"] - i["baseline_shortfall_qty"])
        ratio = round(max([short_by_line[i["change_line_id"]] / i["_line"] for i in flagged], default=0), 4)
        ratio = min(1.0, ratio)
        max_d = max(l["date_dev_days_vs_requested"] for l in lines_out)
        max_q = max(l["qty_dev_pct_vs_requested"] for l in lines_out)
        max_p = max(l["price_dev_pct_vs_requested"] for l in lines_out)
        absv = round(sum(abs(l["value_delta"]) for l in lines_out), 2)
        level, codes = level_for(max_d, max_q, max_p, absv, bool(flagged), max_late, ratio)
        declined = sum(v["qty"] for v in ack["vals"]) == 0
        plan_steps = decide(level, declined, max_p, max_late, ratio)
        sub = t + MIN
        cur, steps = sub + MIN, []
        mgr = rng.choice(managers[ps.po["receiving_warehouse_id"]])
        for no, (role, dec, reason) in enumerate(plan_steps, 1):
            if role == "SYSTEM": hrs, usr = 1 / 60, SYS
            elif role == "PLANNER": hrs, usr = clamp(math.exp(rng.gauss(2.8 if level == "PLANNER" else 2.0, .7)), 1, 72), ps.po["planner_id"]
            else: hrs, usr = clamp(math.exp(rng.gauss(3.6, .7)), 4, 240), mgr
            dec_t = cur + timedelta(seconds=int(hrs * 3600))
            steps.append(dict(no=no, role=role, user=usr, assigned=cur, decided=dec_t, decision=dec, reason=reason))
            cur = dec_t
        fin = steps[-1]
        approved = fin["decision"] == "APPROVED"
        ch = dict(id=cid, ack=ack, sub=sub, lines=lines_out, impacts=impacts, level=level, codes=codes, steps=steps,
                  final_decision=fin["decision"], final_ts=fin["decided"], applied=(fin["decided"] + MIN) if approved else None,
                  superseded=None, max_late=max_late, ratio=ratio, max_d=max_d, max_q=max_q, max_p=max_p, absv=absv,
                  flagged=flagged, declined=declined)
        ch["res_ts"] = ch["applied"] if approved else ch["final_ts"]
        ps.chgs.append(ch)
        counters["chgs"] += 1
        if approved and ch["applied"] <= as_of:
            apr = f"APR-{cid}-{fin['no']:02d}"
            for l in lines_out:
                li = next(i for i, x in enumerate(ps.lines) if x["po_line_id"] == l["po_line_id"])
                ps.plan[li].append(dict(vf=ch["applied"], qty=l["proposed_qty"], price=l["proposed_unit_price"],
                                        date=l["proposed_date"] or l["baseline_date"], chg=cid, appr=apr))
        ack["chg"] = ch
        return ch

    ack_limit = as_of - timedelta(hours=1)  # keeps every derived event (ack+3min) inside the as_of horizon

    # ---- phase A: first acknowledgements -------------------------------------------------
    for ps in POS:
        tr = TIERS[ps.sup["reliability_tier"]]
        if rng.random() < tr["silence"]:
            ps.hard_silent = True
            continue
        h = clamp(rng.gauss(20, 12), 1, SLA_H - 1) if rng.random() < .78 else rng.uniform(SLA_H + 1, 190)
        t = ps.po["issued"] + timedelta(seconds=int(h * 3600))
        if t > ack_limit: continue
        ack = make_ack(ps, t, first_vals(ps))
        make_change(ps, ack, t)
    base_events = counters["acks"] + counters["chgs"]
    if base_events > a.n_events:
        sys.exit(f"baseline events {base_events} exceed --n-events {a.n_events}; raise n-events or lower propensities")

    # ---- phase B: revisions until the exact event budget is hit ---------------------------
    def weight(ps):
        if not ps.acks or len(ps.acks) >= 6: return 0.0
        w = {"A": 1.0, "B": 1.2, "C": 2.2}[ps.sup["reliability_tier"]] * (0.5 ** (len(ps.acks) - 1))
        if ps.chgs and ps.chgs[-1]["final_decision"] == "REJECTED" and ps.chgs[-1]["superseded"] is None: w *= 3
        if ps.acks[-1]["ts"] + timedelta(hours=3) > ack_limit: return 0.0
        return w
    W = [weight(p) for p in POS]
    guard = 0
    while counters["acks"] + counters["chgs"] < a.n_events:
        guard += 1
        if guard > 400000 or sum(W) == 0: sys.exit("could not reach n-events: not enough revisable POs")
        i = rng.choices(range(len(POS)), weights=W)[0]
        ps = POS[i]
        t = ps.acks[-1]["ts"] + timedelta(seconds=int(clamp(math.exp(rng.gauss(3.8, 1.0)), 3, 480) * 3600))
        if t > ack_limit:
            W[i] = 0.0
            continue
        left = a.n_events - counters["acks"] - counters["chgs"]
        lc = ps.chgs[-1] if ps.chgs else None
        if lc is None: state = "none"
        elif lc["superseded"] is None and t >= lc["res_ts"]: state = "rejected" if lc["final_decision"] == "REJECTED" else "approved"
        else: state = "open"
        modes = {"rejected": (["concede", "extend", "reconfirm", "requote"], [55, 20, 15, 10]),
                 "approved": (["extend", "restore", "requote", "reconfirm"], [45, 20, 15, 20]),
                 "none": (["extend", "restore", "requote", "reconfirm"], [45, 20, 15, 20]),
                 "open": (["extend", "concede", "reconfirm"], [40, 30, 30])}[state]
        mode = rng.choices(*modes)[0] if left > 1 else "reconfirm"
        supersede_open(ps, t)
        last = ps.acks[-1]["vals"]
        new = [dict(v) for v in last]

        def plan_vals():
            out = []
            for li in range(len(ps.lines)):
                b = ps.plan_at(li, t)
                out.append(dict(qty=b["qty"], price=b["price"], date=b["date"] if b["qty"] > 0 else None, reason=None))
            return out
        if mode == "concede":
            for v, ln in zip(new, ps.lines):
                if rng.random() < .7:
                    req = ln["requested_qty"]
                    if v["qty"] < req: v["qty"] = req - (req - v["qty"]) // 2
                    if v["qty"] > 0 and v["date"] is None: v["date"] = bd(ln["requested_date"] + timedelta(days=rng.randint(3, 10)))
                    elif v["date"] and v["date"] > ln["requested_date"]:
                        v["date"] = bd(ln["requested_date"] + timedelta(days=(v["date"] - ln["requested_date"]).days // 2))
                    if v["price"] > ln["unit_price"]: v["price"] = round(ln["unit_price"] + (v["price"] - ln["unit_price"]) / 2, 2)
            if new == last: mode = "extend"
        if mode == "restore":
            for v, ln in zip(new, ps.lines):
                if v["qty"] < ln["requested_qty"]:
                    v["qty"] = ln["requested_qty"]
                    v["date"] = bd((v["date"] or ln["requested_date"]) + timedelta(days=rng.randint(0, 5)))
            if new == last: mode = "extend"
        if mode == "requote":
            for v in rng.sample(new, rng.randint(1, len(new))):
                p2 = round(v["price"] * rng.uniform(.97, 1.08), 2)
                v["price"] = p2 if p2 != v["price"] else round(v["price"] + .01, 2)
        if mode == "extend":
            for li in rng.sample(range(len(new)), rng.randint(1, len(new))):
                v, ln = new[li], ps.lines[li]
                if v["qty"] > 0:
                    v["date"] = bd((v["date"] or ln["requested_date"]) + timedelta(days=rng.randint(1, 14)))
                    if rng.random() < .25 and v["qty"] > 1: v["qty"] = max(1, int(v["qty"] * (1 - rng.uniform(.05, .3))))
                else:
                    v["qty"] = max(1, int(ln["requested_qty"] * rng.uniform(.3, .9)))
                    v["date"] = bd(ln["requested_date"] + timedelta(days=rng.randint(5, 21)))
        if mode == "reconfirm": new = plan_vals()
        pv = plan_vals()
        if mode != "reconfirm" and all(n_["qty"] == p_["qty"] and abs(n_["price"] - p_["price"]) < .004 and
                                      (n_["date"] is None or n_["date"] == p_["date"]) for n_, p_ in zip(new, pv)):
            new = pv
        ack = make_ack(ps, t, new)
        make_change(ps, ack, t)
        W[i] = weight(ps)

    # ---- finalisation: rows --------------------------------------------------------------
    NOTE, EVT = [], []
    seq = [0]

    def note(t, typ, rcpt, po, chg, prod, prio, msg):
        if t > as_of: return
        seq[0] += 1
        NOTE.append((t, seq[0], typ, rcpt, po, chg, prod, prio, msg))

    plan_rows = []
    for ps in POS:
        po, pid = ps.po, ps.po["po_id"]
        wh = po["receiving_warehouse_id"]
        issued = po["issued"]
        # lines
        for li, ln in enumerate(ps.lines):
            T["po_line"].append({k: (v.isoformat() if k == "requested_date" else v) for k, v in ln.items() if k != "allocs"})
        # acks
        for ack in ps.acks:
            aid, t = ack["ack_id"], ack["ts"]
            lat = round((t - issued).total_seconds() / 3600, 2)
            vals = ack["vals"]
            T["acknowledgement"].append(dict(
                ack_id=aid, po_id=pid, revision_no=ack["rev"], received_ts=ts(t), channel=ack["channel"],
                ack_status=ack_status_of(vals, ps.lines), total_accepted_qty=sum(v["qty"] for v in vals),
                total_rejected_qty=sum(max(0, l["requested_qty"] - v["qty"]) for v, l in zip(vals, ps.lines)),
                response_latency_hours=lat, is_late=int(lat > SLA_H)))
            GT_LABELS.append(("ACKNOWLEDGEMENT", aid, "ACK_STATUS", ack_status_of(vals, ps.lines)))
            for v, ln in zip(vals, ps.lines):
                alid = f"{aid}-L{ln['line_no']:02d}"
                T["acknowledgement_line"].append(dict(
                    ack_line_id=alid, ack_id=aid, po_line_id=ln["po_line_id"], accepted_qty=v["qty"],
                    rejected_qty=max(0, ln["requested_qty"] - v["qty"]), ack_unit_price=v["price"],
                    promised_date=v["date"].isoformat() if v["date"] else None, rejection_reason=v["reason"]))
        # promises (effective-dated history)
        for li, ln in enumerate(ps.lines):
            rows = []
            for k, ack in enumerate(ps.acks):
                v = ack["vals"][li]
                nxt = ps.acks[k + 1] if k + 1 < len(ps.acks) else None
                if v["qty"] > 0:
                    rows.append(dict(
                        promise_id=f"PRM-{ack['ack_id']}-L{ln['line_no']:02d}", po_line_id=ln["po_line_id"],
                        ack_line_id=f"{ack['ack_id']}-L{ln['line_no']:02d}", revision_no=ack["rev"],
                        promised_date=v["date"].isoformat(), promised_qty=v["qty"], received_ts=ts(ack["ts"]),
                        applied_ts=ts(ack["ts"] + timedelta(seconds=ack["delay"])),
                        valid_to_ts=ts(nxt["ts"] + timedelta(seconds=nxt["delay"])) if nxt else None,
                        is_current=int(nxt is None), days_vs_requested=(v["date"] - ln["requested_date"]).days))
            T["promise_date"] += rows
        # change requests
        for ch in ps.chgs:
            ack, cid = ch["ack"], ch["id"]
            vis = []
            for st in ch["steps"]:
                if st["assigned"] > as_of: break
                vis.append(dict(st, decision="PENDING", decided=None, reason="AWAITING_DECISION") if st["decided"] > as_of else st)
            if ch["superseded"]: status = "SUPERSEDED"
            elif vis[-1]["decision"] == "REJECTED": status = "REJECTED"
            elif vis[-1]["decision"] == "APPROVED": status = "APPLIED" if ch["applied"] and ch["applied"] <= as_of else "APPROVED"
            else: status = "PENDING_APPROVAL"
            resolved = {"SUPERSEDED": ch["superseded"], "REJECTED": ch["final_ts"], "APPLIED": ch["applied"]}.get(status)
            lines = ch["lines"]
            fq, fp, fd = any(l["_qd"] for l in lines), any(l["_prd"] for l in lines), any(l["_dd"] for l in lines)
            ctype = "MULTI" if fq + fp + fd > 1 else "QTY" if fq else "PRICE" if fp else "DATE"
            T["change_request"].append(dict(
                change_id=cid, po_id=pid, ack_id=ack["ack_id"], revision_no=ack["rev"], submitted_ts=ts(ch["sub"]),
                has_qty_change=int(fq), has_price_change=int(fp), has_date_change=int(fd), change_type=ctype,
                changed_line_count=len(lines), max_qty_dev_pct=ch["max_q"], max_price_dev_pct=ch["max_p"],
                max_date_dev_days=ch["max_d"], abs_value_impact=ch["absv"], max_downstream_lateness_days=ch["max_late"],
                max_shortfall_ratio=ch["ratio"], required_approval_level=ch["level"], level_driver="|".join(ch["codes"]),
                current_status=status, resolved_ts=ts(resolved) if resolved else None))
            for l in lines:
                T["change_request_line"].append({k: (v.isoformat() if isinstance(v, date) else v) for k, v in l.items() if not k.startswith("_")})
            for i in ch["impacts"]:
                T["change_impact"].append({k: v for k, v in i.items() if not k.startswith("_")})
            for st in vis:
                T["approval"].append(dict(approval_id=f"APR-{cid}-{st['no']:02d}", change_id=cid, step_no=st["no"], approver_role=st["role"],
                                          approver_user_id=st["user"], assigned_ts=ts(st["assigned"]),
                                          decided_ts=ts(st["decided"]) if st["decided"] else None, decision=st["decision"],
                                          reason_code=st["reason"]))
            # change status events
            ev = [("SUBMITTED", ch["sub"], ack["ack_id"]), ("PENDING_APPROVAL", ch["sub"] + MIN, ack["ack_id"])]
            for st in vis:
                aid_ = f"APR-{cid}-{st['no']:02d}"
                if st["decision"] == "REJECTED": ev.append(("REJECTED", st["decided"], aid_))
                elif st["decision"] == "APPROVED" and st["no"] == len(plan_steps_of(ch)):
                    ev.append(("APPROVED", st["decided"], aid_))
                    if ch["applied"] and not ch["superseded"] and ch["applied"] <= as_of: ev.append(("APPLIED", ch["applied"], aid_))
            if ch["superseded"]:
                nxt_ack = ps.acks[ack["rev"]]["ack_id"]
                ev.append(("SUPERSEDED", ch["superseded"], nxt_ack))
            prev = None
            for k, (to, t, trig) in enumerate([e for e in ev if e[1] <= as_of], 1):
                EVT.append((t, cid, k, "CHANGE_REQUEST", pid, prev, to, trig))
                prev = to
            # labels (clean, rule-derived expectations)
            exp_dec = {"SUPERSEDED": "NONE", "PENDING_APPROVAL": "NONE"}.get(status, "APPROVED" if status in ("APPLIED", "APPROVED") else "REJECTED")
            GT_LABELS += [("CHANGE_REQUEST", cid, "APPROVAL_LEVEL", ch["level"]), ("CHANGE_REQUEST", cid, "FINAL_DECISION", exp_dec),
                          ("CHANGE_REQUEST", cid, "PLAN_UPDATE_ALLOWED", int(status == "APPLIED")),
                          ("CHANGE_REQUEST", cid, "DOWNSTREAM_IMPACT", int(bool(ch["flagged"]))),
                          ("CHANGE_REQUEST", cid, "IMPACTED_PLANNERS", "|".join(sorted({i["impacted_planner_id"] for i in ch["flagged"]})))]
            # notifications
            lvlp = {"AUTO": "LOW", "PLANNER": "NORMAL", "MANAGER": "HIGH"}[ch["level"]]
            note(ch["sub"], "ACK_DEVIATION_RECEIVED", po["planner_id"], pid, cid, None, lvlp,
                 f"{ack['ack_id']} deviates from plan on {len(lines)} line(s): max date dev {ch['max_d']}d, qty dev {ch['max_q']}%, price dev {ch['max_p']}%.")
            for st in vis:
                if st["role"] != "SYSTEM":
                    note(st["assigned"], "APPROVAL_REQUIRED", st["user"], pid, cid, None, "HIGH" if st["role"] == "OPS_MANAGER" else "NORMAL",
                         f"{cid} needs {st['role']} review (level {ch['level']}: {'|'.join(ch['codes'])}).")
            if ch["level"] == "MANAGER":
                note(ch["sub"], "LOGISTICS_REVIEW_REQUESTED", analysts[wh][0], pid, cid, None, "NORMAL",
                     f"Assess logistics impact of {cid} (value impact {ch['absv']}).")
            if status == "REJECTED":
                note(ch["final_ts"], "CHANGE_REJECTED", po["planner_id"], pid, cid, None, "NORMAL",
                     f"{cid} rejected ({vis[-1]['reason']}); follow up with supplier {po['supplier_id']}.")
            if status == "APPLIED":
                t_ap = ch["applied"]
                note(t_ap, "CHANGE_APPLIED", po["planner_id"], pid, cid, None, "NORMAL", f"{cid} approved; projected supply updated.")
                seen = []
                for i in ch["flagged"]:
                    if i["impacted_planner_id"] not in seen:
                        seen.append(i["impacted_planner_id"])
                        n_ord = sum(1 for j in ch["flagged"] if j["impacted_planner_id"] == i["impacted_planner_id"])
                        note(t_ap, "DOWNSTREAM_IMPACT_ALERT", i["impacted_planner_id"], pid, cid, i["production_order_id"],
                             "HIGH" if ch["max_late"] > 7 else "NORMAL",
                             f"{cid} impacts {n_ord} production order(s) you plan (max lateness {ch['max_late']}d).")
                if fq or fd:
                    note(t_ap, "INBOUND_SCHEDULE_UPDATE", rng.choice(operators[wh]), pid, cid, None, "NORMAL",
                         f"Inbound for {pid} changed (qty/date); update dock schedule.")
            GT_LABELS.append(("CHANGE_REQUEST", cid, "FINAL_STATUS", status))
            ch["_status"] = status
        # plan rows
        for li, ln in enumerate(ps.lines):
            vers = [v for v in ps.plan[li] if v["vf"] <= as_of]
            for k, v in enumerate(vers, 1):
                plan_rows.append(dict(po_line_id=ln["po_line_id"], sku_id=ln["sku_id"], warehouse_id=wh, plan_version=k,
                                      projected_qty=v["qty"], projected_date=v["date"].isoformat(), unit_price=v["price"],
                                      valid_from_ts=ts(v["vf"]), valid_to_ts=ts(vers[k]["vf"]) if k < len(vers) else None,
                                      source_change_id=v["chg"], authorizing_approval_id=v["appr"],
                                      update_reason="INITIAL_PO" if k == 1 else "APPROVED_CHANGE"))
        # PO status events
        ev = [("ISSUED", issued, None)]
        first_ts = ps.acks[0]["ts"] if ps.acks else None
        if (first_ts is None or first_ts > issued + timedelta(hours=SLA_H)) and issued + timedelta(hours=SLA_H) <= as_of:
            ev.append(("NO_RESPONSE", issued + timedelta(hours=SLA_H), None))
            note(issued + timedelta(hours=SLA_H), "SUPPLIER_SILENCE", po["planner_id"], pid, None, None, "NORMAL",
                 f"No acknowledgement for {pid} from {po['supplier_id']} within {SLA_H}h SLA.")
            note(issued + timedelta(hours=SLA_H), "SUPPLIER_SILENCE", analysts[wh][0], pid, None, None, "LOW",
                 f"Silence on {pid}; consider chaser/escalation.")
        for ack in ps.acks:
            if ack["chg"]: to = "CHANGE_PENDING"
            else: to = "ACKNOWLEDGED" if ack_status_of(ack["vals"], ps.lines) == "ACCEPTED" else "CONFIRMED"
            ev.append((to, ack["ts"], ack["ack_id"]))
        for ch in ps.chgs:
            st = ch["_status"]
            if st == "APPLIED":
                declined = ch["declined"]
                ev.append(("SUPPLIER_DECLINED" if declined else "CONFIRMED", ch["applied"], f"APR-{ch['id']}-{len(plan_steps_of(ch)):02d}"))
            elif st == "REJECTED":
                ev.append(("CHANGE_REJECTED", ch["final_ts"], f"APR-{ch['id']}-{len(plan_steps_of(ch)):02d}"))
        ev = [e for e in ev if e[1] <= as_of]
        ev.sort(key=lambda e: e[1])
        k, prev = 0, None
        for to, t, trig in ev:
            if to == prev: continue
            k += 1
            EVT.append((t, pid, k, "PURCHASE_ORDER", pid, prev, to, trig))
            prev = to
        po_status = prev
        T["purchase_order"].append(dict(
            po_id=pid, supplier_id=po["supplier_id"], planner_id=po["planner_id"], receiving_warehouse_id=wh,
            order_date=po["order_date"].isoformat(), issued_ts=ts(issued), currency="USD", line_count=len(ps.lines),
            total_amount=round(sum(l["line_amount"] for l in ps.lines), 2), ack_sla_due_ts=ts(issued + timedelta(hours=SLA_H)),
            current_status=po_status, latest_ack_revision=len(ps.acks)))
        GT_LABELS.append(("PURCHASE_ORDER", pid, "PO_STATUS", po_status))
        GT_LABELS.append(("PURCHASE_ORDER", pid, "SUPPLIER_SILENCE_EXCEPTION", int(not ps.acks and issued + timedelta(hours=SLA_H) <= as_of)))

    # plan ids chronological
    plan_rows.sort(key=lambda r: (r["valid_from_ts"], r["po_line_id"], r["plan_version"]))
    for i, r in enumerate(plan_rows, 1): r["inventory_plan_id"] = f"IPL-2026-{i:06d}"
    T["inventory_plan"] = plan_rows
    # status events ids
    EVT.sort(key=lambda e: (e[0], e[1], e[2]))
    for i, (t, ent, k, et, pid, fr, to, trig) in enumerate(EVT, 1):
        T["status_event"].append(dict(status_event_id=f"EVT-2026-{i:06d}", entity_type=et, entity_id=ent, po_id=pid, event_seq=k,
                                      from_status=fr, to_status=to, event_ts=ts(t), trigger_id=trig))
    # notifications ids
    NOTE.sort(key=lambda n: (n[0], n[1]))
    for i, (t, _, typ, rc, po, chg, prod, prio, msg) in enumerate(NOTE, 1):
        rd = t + timedelta(seconds=int(rng.uniform(300, 36 * 3600)))
        read = rng.random() < .9 and rd <= as_of
        T["notification"].append(dict(notification_id=f"NOT-2026-{i:06d}", notification_type=typ, recipient_user_id=rc, po_id=po,
                                      change_id=chg, production_order_id=prod, created_ts=ts(t),
                                      channel=rng.choices(["EMAIL", "IN_APP"], [40, 60])[0], priority=prio,
                                      status="READ" if read else "SENT", read_ts=ts(rd) if read else None, message=msg))

    inject_anomalies(a, rng, as_of, T, POS, ANOM)
    return T, ANOM, GT_LABELS


def plan_steps_of(ch):
    """Number of approval steps originally planned (1 for AUTO/PLANNER, 2 for MANAGER)."""
    return [None] * (2 if ch["level"] == "MANAGER" else 1)


# ----------------------------------------------------------------------------- anomaly injection
def inject_anomalies(a, rng, as_of, T, POS, ANOM):
    n = a.anomalies_per_type if a.anomalies_per_type is not None else max(3, round(a.n_pos * 0.004))
    ack_lines = {r["ack_line_id"]: r for r in T["acknowledgement_line"]}
    promise_by_ackline = {r["ack_line_id"]: r for r in T["promise_date"]}
    ack_po = {r["ack_id"]: r["po_id"] for r in T["acknowledgement"]}
    po_order = {r["po_id"]: r["order_date"] for r in T["purchase_order"]}
    po_line_po = {r["po_line_id"]: r["po_id"] for r in T["po_line"]}
    all_lines = [r["po_line_id"] for r in T["po_line"]]
    used = set()
    by_line = defaultdict(list)  # snapshot grouped by ORIGINAL po_line_id (before any mutation)
    for r in T["promise_date"]: by_line[r["po_line_id"]].append(r)

    def log(issue, table, rid, sec, po, field, orig, inj, rule, sev="HIGH"):
        ANOM.append(dict(anomaly_id=f"GT-{len(ANOM) + 1:05d}", issue_type=issue, table_name=table, record_id=rid,
                         secondary_record_id=sec, po_id=po, field_name=field, original_value=orig, injected_value=inj,
                         detection_rule=rule, severity=sev))

    # 1) acknowledgement refers to wrong PO line (valid FK, wrong parent)
    for alid in rng.sample(sorted(ack_lines), min(n, len(ack_lines))):
        r = ack_lines[alid]
        po = ack_po[r["ack_id"]]
        while True:
            wl = rng.choice(all_lines)
            if po_line_po[wl] != po: break
        log("ACK_REFERS_WRONG_PO_LINE", "acknowledgement_line", alid, f"PRM-{alid}" if alid in promise_by_ackline else None, po,
            "po_line_id", r["po_line_id"], wl, "po_line.po_id <> acknowledgement.po_id")
        r["po_line_id"] = wl
        if alid in promise_by_ackline: promise_by_ackline[alid]["po_line_id"] = wl
        used.add(alid)
    # 2) promised date before order date
    elig = sorted(k for k, r in ack_lines.items() if r["promised_date"] and k not in used)
    for alid in rng.sample(elig, min(n, len(elig))):
        r, pr = ack_lines[alid], promise_by_ackline[alid]
        po = ack_po[r["ack_id"]]
        nd = (date.fromisoformat(po_order[po]) - timedelta(days=rng.randint(1, 14))).isoformat()
        log("PROMISE_DATE_BEFORE_ORDER_DATE", "promise_date", pr["promise_id"], alid, po, "promised_date", pr["promised_date"], nd,
            "promise.promised_date < purchase_order.order_date")
        old = pr["promised_date"]
        r["promised_date"], pr["promised_date"] = nd, nd
        pr["days_vs_requested"] = (date.fromisoformat(nd) - date.fromisoformat(old)).days + pr["days_vs_requested"]
        used.add(alid)
    # 3) older revision overwrites newer revision (stale promise becomes current)
    cands = [k for k in sorted(by_line) if len(by_line[k]) >= 2 and by_line[k][-1]["is_current"] == 1
             and by_line[k][-1]["ack_line_id"] not in used and by_line[k][-2]["ack_line_id"] not in used
             and by_line[k][-2]["revision_no"] == by_line[k][-1]["revision_no"] - 1]
    for lid in rng.sample(cands, min(n, len(cands))):
        old, new = by_line[lid][-2], by_line[lid][-1]
        t_new = datetime.fromisoformat(new["applied_ts"])
        o_applied = ts(t_new + timedelta(minutes=30))
        log("OLDER_REVISION_OVERWRITES_NEWER", "promise_date", old["promise_id"], new["promise_id"], po_line_po[lid], "is_current",
            "0", "1", "is_current row has revision_no < max revision for the same po_line")
        old["applied_ts"], old["valid_to_ts"], old["is_current"] = o_applied, None, 1
        new["valid_to_ts"], new["is_current"] = o_applied, 0
        used.add(old["ack_line_id"]); used.add(new["ack_line_id"])
    # 4) unapproved change updates inventory plan
    plan_by_line = defaultdict(list)
    for r in T["inventory_plan"]: plan_by_line[r["po_line_id"]].append(r)
    appr_by_chg = defaultdict(list)
    for r in T["approval"]: appr_by_chg[r["change_id"]].append(r)
    lines_by_chg = defaultdict(list)
    for r in T["change_request_line"]: lines_by_chg[r["change_id"]].append(r)
    cands = []
    for c in T["change_request"]:
        if c["current_status"] not in ("REJECTED", "PENDING_APPROVAL", "SUPERSEDED"): continue
        sub = datetime.fromisoformat(c["submitted_ts"])
        if c["current_status"] == "REJECTED": t_inj = datetime.fromisoformat(c["resolved_ts"]) + MIN
        elif c["current_status"] == "PENDING_APPROVAL": t_inj = sub + timedelta(minutes=90)
        else:
            gap = datetime.fromisoformat(c["resolved_ts"]) - sub
            if gap < timedelta(minutes=10): continue
            t_inj = sub + gap / 2
        t_inj = t_inj.replace(microsecond=0)
        if t_inj > as_of: continue
        l0 = lines_by_chg[c["change_id"]][0]
        last = plan_by_line[l0["po_line_id"]][-1]
        if last["valid_to_ts"] is None and datetime.fromisoformat(last["valid_from_ts"]) < t_inj:
            cands.append((c["change_id"], t_inj, l0, last))
    nxt, done, hit_lines = len(T["inventory_plan"]), 0, set()
    for ci in rng.sample(range(len(cands)), len(cands)):
        if done >= n: break
        cid, t_inj, l0, last = cands[ci]
        if l0["po_line_id"] in hit_lines: continue
        hit_lines.add(l0["po_line_id"]); done += 1
        nxt += 1
        row = dict(inventory_plan_id=f"IPL-2026-{nxt:06d}", po_line_id=l0["po_line_id"], sku_id=last["sku_id"], warehouse_id=last["warehouse_id"],
                   plan_version=last["plan_version"] + 1, projected_qty=l0["proposed_qty"], projected_date=l0["proposed_date"] or last["projected_date"],
                   unit_price=l0["proposed_unit_price"], valid_from_ts=ts(t_inj), valid_to_ts=None, source_change_id=cid,
                   authorizing_approval_id=appr_by_chg[cid][-1]["approval_id"], update_reason="APPROVED_CHANGE")
        st = next(c["current_status"] for c in T["change_request"] if c["change_id"] == cid)
        log("UNAPPROVED_CHANGE_UPDATES_INVENTORY_PLAN", "inventory_plan", row["inventory_plan_id"], last["inventory_plan_id"],
            l0["po_line_id"].rsplit("-L", 1)[0], "source_change_id", "", cid,
            f"inventory_plan.source_change_id references change with status {st} (not APPLIED)")
        last["valid_to_ts"] = row["valid_from_ts"]
        T["inventory_plan"].append(row)
        plan_by_line[l0["po_line_id"]].append(row)


# ----------------------------------------------------------------------------- export
def csv_val(v):
    return "" if v is None else str(v)


def write_all(a, T, ANOM, LABELS):
    out = a.out
    for d in ("data", "ground_truth", "sql", "docs"): os.makedirs(os.path.join(out, d), exist_ok=True)
    formats = a.formats.split(",")
    sums, counts = {}, {}
    schema = {t["name"]: t for t in S.TABLES}
    # verify columns
    for name, rows in T.items():
        cols = [f["name"] for f in schema[name]["fields"]]
        for r in rows[:200] + rows[-200:]:
            assert set(r) == set(cols), (name, set(r) ^ set(cols))
        counts[name] = len(rows)

    def wcsv(path, cols, rows):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(cols)
            for r in rows: w.writerow([csv_val(r.get(c)) for c in cols])
        sums[os.path.relpath(path, out)] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    for name, rows in T.items():
        cols = [f["name"] for f in schema[name]["fields"]]
        if "csv" in formats: wcsv(os.path.join(out, "data", f"{name}.csv"), cols, rows)
        if "json" in formats:
            p = os.path.join(out, "data", f"{name}.json")
            with open(p, "w", encoding="utf-8") as fh: json.dump([{c: r.get(c) for c in cols} for r in rows], fh, separators=(",", ":"))
            sums[os.path.relpath(p, out)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    # ground truth (separate folder; never joined into data/)
    acols = ["anomaly_id", "issue_type", "table_name", "record_id", "secondary_record_id", "po_id", "field_name",
             "original_value", "injected_value", "detection_rule", "severity"]
    wcsv(os.path.join(out, "ground_truth", "ground_truth_anomalies.csv"), acols, ANOM)
    lrows = [dict(entity_type=e, entity_id=i, label_name=n, label_value=v) for e, i, n, v in LABELS]
    wcsv(os.path.join(out, "ground_truth", "ground_truth_labels.csv"), ["entity_type", "entity_id", "label_name", "label_value"], lrows)
    p = os.path.join(out, "ground_truth", "ground_truth.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(dict(note="Evaluation only - never use as model/pipeline input.", rules=S.RULES, anomalies=ANOM, labels=lrows), fh, separators=(",", ":"))
    sums[os.path.relpath(p, out)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    # SQL
    if "sql" in formats:
        with open(os.path.join(out, "sql", "schema.sql"), "w") as fh: fh.write(S.ddl())
        p = os.path.join(out, "sql", "seed_data.sql")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("PRAGMA foreign_keys=OFF;\nBEGIN;\n")
            for name in S.TABLE_ORDER:
                cols = [f["name"] for f in schema[name]["fields"]]
                rows = T[name]
                for i in range(0, len(rows), 500):
                    vals = []
                    for r in rows[i:i + 500]:
                        vals.append("(" + ",".join("NULL" if r.get(c) is None else (str(r[c]) if isinstance(r[c], (int, float)) else "'" + str(r[c]).replace("'", "''") + "'") for c in cols) + ")")
                    fh.write(f"INSERT INTO {name} ({','.join(cols)}) VALUES\n" + ",\n".join(vals) + ";\n")
            fh.write("COMMIT;\n")
        for q in ("schema.sql", "seed_data.sql"):
            sums[f"sql/{q}"] = hashlib.sha256(open(os.path.join(out, "sql", q), "rb").read()).hexdigest()
    # docs
    with open(os.path.join(out, "docs", "data_dictionary.md"), "w") as fh: fh.write(S.data_dictionary_md())
    with open(os.path.join(out, "docs", "data_dictionary.csv"), "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["table", "field", "type", "required", "key", "allowed_values_or_format", "unit", "rule"])
        for t in S.TABLES:
            for f in t["fields"]: w.writerow([t["name"], f["name"], f["type"], "Y" if f["required"] else "N", f["key"], f["domain"], f["unit"], f["rule"]])
    with open(os.path.join(out, "docs", "schema.json"), "w") as fh:
        json.dump(dict(tables=S.TABLES, po_transitions=S.ALLOWED_PO_TRANSITIONS, chg_transitions=S.ALLOWED_CHG_TRANSITIONS,
                       table_order=S.TABLE_ORDER), fh, indent=1)
    with open(os.path.join(out, "docs", "rules.json"), "w") as fh: json.dump(S.RULES, fh, indent=1)
    cmd = f"python3 generate_data.py --seed {a.seed} --n-pos {a.n_pos} --n-events {a.n_events} --as-of {a.as_of} --formats {a.formats} --out <dir>"
    if a.anomalies_per_type is not None: cmd += f" --anomalies-per-type {a.anomalies_per_type}"
    manifest = dict(generator="generate_data.py", version=VERSION, command=cmd, seed=a.seed,
                    parameters=dict(n_pos=a.n_pos, n_events=a.n_events, as_of=a.as_of, anomalies_per_type=a.anomalies_per_type, formats=a.formats),
                    row_counts=counts, sha256=dict(sorted(sums.items())), anomalies_injected={k: sum(1 for x in ANOM if x["issue_type"] == k) for k in sorted({x["issue_type"] for x in ANOM})})
    with open(os.path.join(out, "docs", "generation_manifest.json"), "w") as fh: json.dump(manifest, fh, indent=1)
    src = dict(external_data_used=False,
               statement="All records are fully synthetic, produced by generate_data.py from a seeded PRNG. No third-party dataset was downloaded, "
                         "scraped or redistributed; no personal or confidential data is included (names are random combinations of common given/family names, "
                         "supplier names are invented).",
               schema_inspiration=["Public descriptions of EDI X12 855 (PO Acknowledgment), 865 (PO Change Acknowledgment) and UN/EDIFACT ORDRSP message purposes - "
                                   "referenced for field naming only; no files or data retrieved."],
               fields_discarded=[], transformations=[], license="Generated data released by the author for evaluation use.")
    with open(os.path.join(out, "docs", "source_manifest.json"), "w") as fh: json.dump(src, fh, indent=1)
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--n-pos", type=int, default=10000)
    ap.add_argument("--n-events", type=int, default=15000, help="acknowledgements + change requests (exact)")
    ap.add_argument("--as-of", default="2026-10-22T18:00:00")
    ap.add_argument("--anomalies-per-type", type=int, default=None, help="default max(3, 0.4%% of POs)")
    ap.add_argument("--formats", default="csv,json,sql")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    T, ANOM, LABELS = generate(a)
    m = write_all(a, T, ANOM, LABELS)
    print(json.dumps(m["row_counts"], indent=1))
    print("events (ack+chg):", m["row_counts"]["acknowledgement"] + m["row_counts"]["change_request"])
    print("anomalies:", m["anomalies_injected"])


if __name__ == "__main__":
    main()
