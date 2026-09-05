import io, os, sys
sys.path.insert(0, "src")
import file_agent
# This was used to analyse real-world ID collision rates.
# It points to a local tmp directory.
D = "C:/Users/rishi/AppData/Local/Temp/ai_workspace/D--PROJECTS-AmongResolver/4b17fedf-c939-415e-8530-9e73603a49e5/scratchpad/realdata"
print(f"{'feed':<14}{'rows':>7}{'distinct txn_id':>17}{'collision rate':>16}  worst duplicate")
print("-"*82)
for name in ["chicago","vermont","cincinnati","mesa","nyc_payments"]:
    raw = io.open(f"{D}/{name}.csv", encoding="utf-8", errors="replace").read()
    raw = "\n".join(raw.splitlines()[:4001])
    rows = file_agent.parse_csv(raw, name+".csv")
    if not rows: continue
    ids = [str(r.get("txn_id") or "") for r in rows]
    from collections import Counter
    c = Counter(ids)
    distinct = len(c)
    dup_rate = 1 - distinct/len(ids)
    worst, n = c.most_common(1)[0]
    print(f"{name:<14}{len(ids):>7}{distinct:>17}{dup_rate:>15.1%}  {n}x {worst[:34]!r}")
