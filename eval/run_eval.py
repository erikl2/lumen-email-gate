"""Run our generality set (or any practice-format JSON) through the real mock + wrapper: python3 eval/run_eval.py [file.json] [-v]"""
import sys, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from harness.runner import Stack
from harness.judging import call_decide, metrics
from concurrent.futures import ThreadPoolExecutor
items = json.load(open(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else str(__import__("pathlib").Path(__file__).with_name("generality_set.json"))))["items"]
with Stack(latency="0") as s:
    with ThreadPoolExecutor(8) as pool:
        res = list(pool.map(lambda it: call_decide(s.wrapper_url, it, "customer_support", 60), items))
for it, r in zip(items, res):
    got = r.get("decision", "ERROR")
    exp = it["expected_decision"]
    mark = "ok" if got == exp else ("~~" if it["label"] == "bad" and got != "APPROVE" else "XX")
    if mark != "ok" or "-v" in sys.argv:
        print(f"{mark} {it['id']:8} exp {exp[:5]} got {got[:5]} ctx={it['context']} | {it['text'][:110]}\n     why-label: {it['reason'][:120]}\n     ours: {r.get('reason','')[:220]}")
m = metrics(list(zip(items, res)))
print(f"\ncaught {m['caught']}/{m['bad']}  false blocks {m['false_blocks']}/{m['good']}  exact {m['exact']}/{m['items']}  errors {m['errors']}")
