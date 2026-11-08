import argparse, html, re, time
from pathlib import Path
import pandas as pd, requests

GAP = 3.5
VER_RE = re.compile(r"\[v(\d+)\]\s*(.+?UTC)\s*\(([\d.,]+)\s*([KM]B)\)\s*(\(withdrawn\))?", re.I)

def fetch(s, url, stream=False, tries=4):
    for i in range(tries):
        try:
            r = s.get(url, timeout=60, stream=stream)
        except requests.RequestException:
            time.sleep(5 * (i + 1)); continue
        if r.status_code in (429, 500, 502, 503, 504):
            r.close(); time.sleep(10 * (i + 1)); continue
        time.sleep(GAP)
        return r
    return None

def parse_history(page):
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    hist = {}
    for v, _date, size, unit, wd in VER_RE.findall(text):
        kb = float(size.replace(",", "")) * (1024 if unit.upper() == "MB" else 1)
        hist[f"v{v}"] = (kb, bool(wd))
    return hist

def probe(s, aid, v):
    out = []
    for kind in ("e-print", "pdf"):
        r = fetch(s, f"https://arxiv.org/{kind}/{aid}{v}", stream=True)
        out.append(f"{kind}={getattr(r, 'status_code', 'none')}")
        if r is not None:
            r.close()
    return " ".join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag-dir", required=True)
    ap.add_argument("--out", default="./R8_retry")
    ap.add_argument("--email", required=True)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    papers = pd.read_csv(Path(a.diag_dir) / "paper_diagnosis.csv", dtype=str).fillna("")
    vers = pd.read_csv(Path(a.diag_dir) / "version_manifests.csv", dtype=str).fillna("")
    failed_map = (vers[vers["download_status"] == "failed"]
                  .groupby("arxiv_id")["version"].apply(list).to_dict())

    s = requests.Session()
    s.headers["User-Agent"] = f"intro2DS-student-crawler/1.0 (mailto:{a.email})"

    rows = []
    for i, p in enumerate(papers.itertuples(index=False), 1):
        aid, diag = p.arxiv_id, p.primary_diagnosis
        r = fetch(s, f"https://arxiv.org/abs/{aid}")
        if r is None or r.status_code != 200:
            rows.append(dict(arxiv_id=aid, diagnosis=diag, verdict="ABS_FETCH_FAILED",
                             detail=getattr(r, "status_code", "none")))
            print(f"[{i}/45] {aid} ABS_FETCH_FAILED"); continue
        hist = parse_history(r.text)
        withdrawn = {v for v, (_, w) in hist.items() if w}
        failed = failed_map.get(aid, [])
        detail = "; ".join(f"{v}:{kb:.0f}KB{'(withdrawn)' if w else ''}" for v, (kb, w) in hist.items())
        if failed:
            unexplained = [v for v in failed if v not in withdrawn]
            if not hist:
                verdict = "PARSE_FAILED_CHECK_HTML"
            elif not unexplained:
                verdict = "WITHDRAWN_VERSION"
            else:
                verdict = "UNEXPLAINED"
                detail += " | probe " + "; ".join(f"{v}: {probe(s, aid, v)}" for v in unexplained)
        elif diag == "SEMANTIC_SCHOLAR_REFERENCE_FETCH_FAILED":
            verdict = "S2_FAIL_PAPER_HAS_WITHDRAWN_VERSION" if withdrawn else "S2_FAIL_NOT_WITHDRAWN"
        else:
            verdict = "OTHER_CHECK_MANUALLY"
        rows.append(dict(arxiv_id=aid, diagnosis=diag, failed_versions=",".join(failed),
                         verdict=verdict, detail=detail))
        print(f"[{i}/45] {aid} {verdict} | {detail}")
        pd.DataFrame(rows).to_csv(out / "abs_classification.csv", index=False)

    print(pd.DataFrame(rows)["verdict"].value_counts().to_string())

if __name__ == "__main__":
    main()