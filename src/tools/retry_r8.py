"""
retry_r8.py  --  thử cứu lại các paper R8 bị failed
Cách chạy:
    pip install requests pandas
    python retry_r8.py --diag-dir ./R8_diagnosis --out ./R8_retry --email you@example.com
    (tuỳ chọn) --s2-key <SEMANTIC_SCHOLAR_API_KEY>
"""
import argparse, gzip, io, json, re, shutil, tarfile, time
from pathlib import Path
import pandas as pd
import requests

ARXIV_GAP = 3.5      # arXiv yêu cầu >= 3s giữa các request
S2_GAP = 1.2

def get(session, url, max_try=5, gap=ARXIV_GAP, **kw):
    """GET có retry/backoff, tôn trọng Retry-After. Trả về response cuối cùng."""
    r = None
    for i in range(max_try):
        try:
            r = session.get(url, timeout=60, **kw)
        except requests.RequestException as e:
            wait = 5 * (i + 1)
            print(f"    lỗi mạng ({e.__class__.__name__}), thử lại sau {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", 10 * (i + 1)))
            print(f"    HTTP {r.status_code}, chờ {wait}s")
            time.sleep(wait)
            continue
        time.sleep(gap)
        return r
    return r

def safe_extract_tar(data: bytes, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        root = dest.resolve()
        for m in tf.getmembers():
            if not (dest / m.name).resolve().is_relative_to(root):
                raise ValueError(f"đường dẫn nguy hiểm trong tar: {m.name}")
        tf.extractall(dest)

def classify_and_save(data: bytes, vdir: Path):
    """Nhận diện định dạng source arXiv trả về, lưu + giải nén. Trả (format, extract_status, note)."""
    vdir.mkdir(parents=True, exist_ok=True)
    if data[:4] == b"%PDF":
        (vdir / "source.pdf").write_bytes(data)
        return "pdf", "empty", "chỉ có PDF, không có TeX"
    if data[:2] == b"\x1f\x8b":
        (vdir / "source.tar.gz").write_bytes(data)
        try:
            safe_extract_tar(data, vdir / "extracted")
            n_tex = len(list((vdir / "extracted").rglob("*.tex")))
            return "tar.gz", ("ok" if n_tex else "empty"), f"{n_tex} file .tex"
        except tarfile.ReadError:
            # gzip đơn (1 file .tex nén)
            try:
                txt = gzip.decompress(data)
                (vdir / "extracted").mkdir(exist_ok=True)
                (vdir / "extracted" / "main.tex").write_bytes(txt)
                return "gzip-single", "ok", "1 file tex (gzip đơn)"
            except Exception as e:
                return "tar.gz", "failed", f"giải nén lỗi: {e}"
        except Exception as e:
            return "tar.gz", "failed", f"giải nén lỗi: {e}"
    (vdir / "source.unknown").write_bytes(data)
    return "unknown", "failed", f"magic bytes lạ: {data[:8]!r}"

def retry_source(session, arxiv_id, ver, out_root):
    vdir = out_root / "papers" / arxiv_id.replace(".", "-") / "versions" / ver
    row = dict(arxiv_id=arxiv_id, version=ver, result="", http="", fmt="", extract="", note="")
    attempts = [
        ("eprint", f"https://arxiv.org/e-print/{arxiv_id}{ver}"),
        ("export-mirror", f"https://export.arxiv.org/e-print/{arxiv_id}{ver}"),
    ]
    for name, url in attempts:
        r = get(session, url)
        row["http"] = getattr(r, "status_code", "no-response")
        if r is not None and r.status_code == 200 and r.content:
            fmt, ext, note = classify_and_save(r.content, vdir)
            row.update(result="DOWNLOADED", fmt=fmt, extract=ext, note=f"{name}: {note}")
            return row
    # không tải được -> kiểm tra version đó có thật không
    r = get(session, f"https://arxiv.org/abs/{arxiv_id}{ver}")
    if r is not None and r.status_code == 404:
        row.update(result="VERSION_DOES_NOT_EXIST", note="abs page của version này trả 404")
    elif r is not None and r.status_code == 200:
        html = r.text
        withdrawn = "withdrawn" in html.lower()
        pdf_r = get(session, f"https://arxiv.org/pdf/{arxiv_id}{ver}")
        if pdf_r is not None and pdf_r.status_code == 200 and pdf_r.content[:4] == b"%PDF":
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "source.pdf").write_bytes(pdf_r.content)
            row.update(result="PDF_ONLY_FALLBACK", fmt="pdf", extract="empty",
                       note="version tồn tại, e-print lỗi, lấy được PDF" + (" (withdrawn?)" if withdrawn else ""))
        else:
            row.update(result="STILL_FAILED", note=f"abs OK nhưng e-print/pdf lỗi (last http={row['http']})")
    else:
        row.update(result="STILL_FAILED", note=f"abs http={getattr(r,'status_code','?')}")
    return row

def retry_s2_refs(session, arxiv_id, out_root, s2_key=None):
    headers = {"x-api-key": s2_key} if s2_key else {}
    pdir = out_root / "papers" / arxiv_id.replace(".", "-")
    pdir.mkdir(parents=True, exist_ok=True)
    refs, offset, row = [], 0, dict(arxiv_id=arxiv_id, result="", n_refs=0, note="")
    while True:
        url = (f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}/references"
               f"?fields=title,year,authors,externalIds&limit=500&offset={offset}")
        r = get(session, url, gap=S2_GAP, headers=headers)
        if r is None:
            row.update(result="STILL_FAILED", note="không có response"); return row
        if r.status_code == 404:
            row.update(result="NOT_INDEXED_BY_S2", note="S2 chưa có paper này"); return row
        if r.status_code != 200:
            row.update(result="STILL_FAILED", note=f"http {r.status_code}: {r.text[:150]}"); return row
        j = r.json()
        refs += j.get("data", [])
        if j.get("next") is None:
            break
        offset = j["next"]
    (pdir / "s2_references.json").write_text(json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")
    row.update(result="OK" if refs else "OK_BUT_EMPTY", n_refs=len(refs))
    return row

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag-dir", required=True)
    ap.add_argument("--out", default="./R8_retry")
    ap.add_argument("--email", default="your_email@example.com", help="để vào User-Agent")
    ap.add_argument("--s2-key", default=None)
    a = ap.parse_args()

    diag = Path(a.diag_dir); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    papers = pd.read_csv(diag / "paper_diagnosis.csv", dtype=str).fillna("")
    vers = pd.read_csv(diag / "version_manifests.csv", dtype=str).fillna("")

    sess = requests.Session()
    sess.headers["User-Agent"] = f"intro2DS-student-crawler/1.0 (mailto:{a.email})"

    # 1) các version tải lỗi
    failed_v = vers[vers["download_status"] == "failed"][["arxiv_id", "version"]].drop_duplicates()
    print(f"== Thử lại {len(failed_v)} version tải lỗi ==")
    src_rows = []
    for i, (aid, ver) in enumerate(failed_v.itertuples(index=False), 1):
        print(f"[{i}/{len(failed_v)}] {aid}{ver}")
        row = retry_source(sess, aid, ver, out)
        print("   ->", row["result"], row["note"])
        src_rows.append(row)
        pd.DataFrame(src_rows).to_csv(out / "retry_source_report.csv", index=False)

    # 2) các paper lỗi Semantic Scholar
    s2_ids = papers[papers["primary_diagnosis"] == "SEMANTIC_SCHOLAR_REFERENCE_FETCH_FAILED"]["arxiv_id"].tolist()
    print(f"\n== Thử lại {len(s2_ids)} paper lỗi Semantic Scholar ==")
    s2_rows = []
    for i, aid in enumerate(s2_ids, 1):
        print(f"[{i}/{len(s2_ids)}] {aid}")
        row = retry_s2_refs(sess, aid, out, a.s2_key)
        print("   ->", row["result"], row["n_refs"], row["note"])
        s2_rows.append(row)
        pd.DataFrame(s2_rows).to_csv(out / "retry_s2_report.csv", index=False)

    # 3) paper có version download OK nhưng extract lỗi (2507.05323 v2)
    print("\n== Tóm tắt ==")
    if src_rows:
        print(pd.DataFrame(src_rows)["result"].value_counts().to_string())
    if s2_rows:
        print(pd.DataFrame(s2_rows)["result"].value_counts().to_string())

if __name__ == "__main__":
    main()