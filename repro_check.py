#!/usr/bin/env python3
"""Step 10: regenerate twice with the same seed (plus once with a different seed) and compare row counts + SHA-256 of every output.
    python3 repro_check.py [--seed 20261022 --n-pos 10000 --n-events 15000]
"""
import argparse, json, os, subprocess, sys, tempfile

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=20261022)
ap.add_argument("--n-pos", type=int, default=10000)
ap.add_argument("--n-events", type=int, default=15000)
ap.add_argument("--report", default="output/docs/reproducibility_report.json")
a = ap.parse_args()
here = os.path.dirname(os.path.abspath(__file__))


def run(seed, tag):
    d = tempfile.mkdtemp(prefix=f"yf_{tag}_")
    subprocess.run([sys.executable, os.path.join(here, "generate_data.py"), "--seed", str(seed), "--n-pos", str(a.n_pos),
                    "--n-events", str(a.n_events), "--out", d], check=True, stdout=subprocess.DEVNULL)
    return json.load(open(os.path.join(d, "docs", "generation_manifest.json")))


m1, m2, m3 = run(a.seed, "run1"), run(a.seed, "run2"), run(a.seed + 1, "other_seed")
same_counts = m1["row_counts"] == m2["row_counts"]
diff = [k for k in m1["sha256"] if m1["sha256"][k] != m2["sha256"].get(k)]
rep = dict(seed=a.seed, files_compared=len(m1["sha256"]), row_counts_identical=same_counts, all_sha256_identical=not diff, mismatching_files=diff,
           row_counts_run1=m1["row_counts"], row_counts_run2=m2["row_counts"],
           control_other_seed=dict(seed=a.seed + 1, files_differing=sum(1 for k in m1["sha256"] if m1["sha256"][k] != m3["sha256"].get(k)),
                                   note="Different seed must change data (shows the checksum test can fail)."),
           sample_sha256={k: m1["sha256"][k] for k in sorted(m1["sha256"])[:6]})
os.makedirs(os.path.dirname(os.path.join(here, a.report)), exist_ok=True)
json.dump(rep, open(os.path.join(here, a.report), "w"), indent=1)
print(json.dumps({k: rep[k] for k in ("files_compared", "row_counts_identical", "all_sha256_identical", "mismatching_files", "control_other_seed")}, indent=1))
sys.exit(0 if (same_counts and not diff) else 1)
