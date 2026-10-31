"""End-to-end behaviour tests. Each test generates a SMALL dataset (300 POs) in a temp dir - no pre-built data needed."""
import json, pathlib, subprocess, sys
import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def gen(out, seed=20261022, n=300, ev=450):
    subprocess.run([sys.executable, str(ROOT / "generate_data.py"), "--seed", str(seed), "--n-pos", str(n), "--n-events", str(ev),
                    "--out", str(out), "--formats", "csv,sql"], check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    return gen(tmp_path_factory.mktemp("ds"))


def rd(ds, n): return pd.read_csv(ds / "data" / f"{n}.csv")


def test_exact_volume_and_ids(ds):
    assert len(rd(ds, "purchase_order")) == 300
    assert len(rd(ds, "acknowledgement")) + len(rd(ds, "change_request")) == 450      # ack+change events hit the budget exactly
    assert rd(ds, "purchase_order").po_id.iloc[0] == "PO-2026-000001"
    assert rd(ds, "acknowledgement").ack_id.iloc[0].startswith("ACK-PO-2026-000001-01")


def test_validator_passes_with_zero_errors(ds):
    p = subprocess.run([sys.executable, str(ROOT / "validate.py"), "--root", str(ds)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-2000:]
    s = json.load(open(ds / "docs" / "validation_summary.json"))
    assert s["overall"] == "PASS" and s["error_checks_failed"] == 0


def test_only_approved_changes_update_plan_except_labelled_injections(ds):
    plan, chg = rd(ds, "inventory_plan"), rd(ds, "change_request")
    gt = pd.read_csv(ds / "ground_truth" / "ground_truth_anomalies.csv")
    bad = plan[plan.source_change_id.notna()].merge(chg, left_on="source_change_id", right_on="change_id")
    bad = set(bad[bad.current_status != "APPLIED"].inventory_plan_id)
    injected = set(gt[gt.issue_type == "UNAPPROVED_CHANGE_UPDATES_INVENTORY_PLAN"].record_id)
    assert bad == injected and len(injected) >= 3


def test_exception_detectors_match_ground_truth_exactly(ds):
    subprocess.run([sys.executable, str(ROOT / "validate.py"), "--root", str(ds)], capture_output=True)
    det = pd.read_csv(ds / "docs" / "detected_exceptions.csv")
    gt = pd.read_csv(ds / "ground_truth" / "ground_truth_anomalies.csv")
    for it, g in gt.groupby("issue_type"):
        assert set(det[det.issue_type == it].record_id) == set(g.record_id), it


def test_ground_truth_is_separate_from_inputs(ds):
    for f in (ds / "data").glob("*.csv"):
        cols = set(pd.read_csv(f, nrows=1).columns)
        assert not cols & {"issue_type", "anomaly_id", "label_name", "label_value"}, f.name


def test_revision_history_preserved(ds):
    a = rd(ds, "acknowledgement")
    assert (a.groupby("po_id").revision_no.apply(lambda s: sorted(s) == list(range(1, len(s) + 1)))).all()
    assert a.revision_no.max() >= 2          # multiple revisions exist


def test_supplier_silence_and_partial_acceptance_present(ds):
    po, al = rd(ds, "purchase_order"), rd(ds, "acknowledgement_line")
    assert (po.latest_ack_revision == 0).any()
    assert (al.rejected_qty > 0).any()


def test_same_seed_reproducible_different_seed_differs(ds, tmp_path):
    again = gen(tmp_path / "again")
    m1 = json.load(open(ds / "docs" / "generation_manifest.json"))["sha256"]
    m2 = json.load(open(again / "docs" / "generation_manifest.json"))["sha256"]
    assert m1 == m2
    other = gen(tmp_path / "other", seed=1)
    assert json.load(open(other / "docs" / "generation_manifest.json"))["sha256"] != m1
