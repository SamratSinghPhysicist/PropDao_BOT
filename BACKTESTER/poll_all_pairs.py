import os
import sys
import time
import requests

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.github_runner import resolve_github_token, get_git_remote_repo

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

owner, repo = get_git_remote_repo()
tok = resolve_github_token()
hdrs = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}

url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/pair_backtest.yml/runs?per_page=15"
r = requests.get(url, headers=hdrs)
runs = r.json().get("workflow_runs", [])

print(f"{'Run ID':13s} {'Symbol':16s} {'Status':12s} {'Conclusion':12s} {'Jobs Status'}")
print("-" * 85)

for run in runs:
    rid = run['id']
    st = run.get('status')
    conc = run.get('conclusion') or ""
    
    jr = requests.get(f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{rid}/jobs", headers=hdrs)
    jobs = jr.json().get('jobs', [])
    sym = "unknown"
    job_statuses = []
    for j in jobs:
        name = j.get('name', '')
        if '(' in name and ')' in name:
            sym = name.split('(')[1].split(')')[0]
        
        j_short = name.replace(f" ({sym})", "").replace("Worker ", "").replace("Consolidate ", "Consol:")
        jst = j.get('status')
        jconc = j.get('conclusion')
        icon = "[OK]" if jconc == "success" else ("[FAIL]" if jconc == "failure" else ("[RUN]" if jst == "in_progress" else "[QUE]"))
        job_statuses.append(f"{j_short}:{icon}")
        
    jobs_summary = " ".join(job_statuses)
    print(f"#{rid:<12d} {sym:16s} {st:12s} {conc:12s} {jobs_summary}")
