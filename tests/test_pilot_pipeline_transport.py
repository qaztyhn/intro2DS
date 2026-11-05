"""Two-paper end-to-end integration: actual variant driver, mocked adapters."""

import gzip
import io
import json
import sys
import tarfile
from urllib.parse import parse_qs, urlparse

import requests
import yaml

from src import main, metrics
from src.utils import http


IDS = ["2505.16300", "2505.16447"]


def atom_feed(ids):
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{pid}v1</id><title>SYNTHETIC transport test</title>"
        "<published>2025-05-01T00:00:00Z</published><updated>2025-05-01T00:00:00Z</updated>"
        "<author><name>Invented Author</name></author><summary>Offline fixture</summary></entry>"
        for pid in ids)
    return ("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:opensearch='http://a9.com/-/spec/opensearch/1.1/'>"
            f"<opensearch:totalResults>{len(ids)}</opensearch:totalResults>"
            f"<opensearch:itemsPerPage>{len(ids)}</opensearch:itemsPerPage>"
            f"<opensearch:startIndex>0</opensearch:startIndex>{entries}</feed>").encode()


def test_variant_pipeline_transport_pagination_retry_and_404(tmp_path, monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        data = b"\\documentclass{article}\n\\begin{document}SYNTHETIC\\end{document}"
        member = tarfile.TarInfo("main.tex")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    source = gzip.compress(payload.getvalue())
    calls, page_attempts = [], {}

    def send(adapter, prepared, **kwargs):
        parsed = urlparse(prepared.url)
        query = parse_qs(parsed.query)
        calls.append((prepared.url, kwargs.get("timeout")))
        status = 200
        if parsed.path == "/api/query":
            body = atom_feed(query["id_list"][0].split(","))
        elif parsed.path.startswith("/src/"):
            body = source
        elif parsed.path.endswith("/references"):
            pid = parsed.path.split("ARXIV:")[1].split("/")[0]
            offset = int(query["offset"][0])
            key = (pid, offset)
            page_attempts[key] = page_attempts.get(key, 0) + 1
            if pid == IDS[1]:
                status, body = 404, b'{}'
            elif offset == 1 and page_attempts[key] == 1:
                status, body = 429, b'{}'
            else:
                page = {"offset": offset, "data": [{"citedPaper": {
                    "paperId": f"SYNTHETIC-{offset}", "title": "Invented reference"}}]}
                if offset == 0:
                    page["next"] = 1
                body = json.dumps(page).encode()
        else:
            raise AssertionError(f"Unexpected offline request: {prepared.url}")
        response = requests.Response()
        response.status_code, response._content = status, body
        response._content_consumed = True
        response.request, response.url = prepared, prepared.url
        response.headers["Retry-After"] = "0"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    variant = "A" if hasattr(main, "discover_batches") else "B" if hasattr(main, "run_papers") else "B0"
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("\n".join(IDS) + "\n")
    cfg = {"run_id": "synthetic-pipeline", "config_name": "A_BATCH" if variant == "A" else "B",
           "schema_version": 1, "ids_file": str(ids_file), "output_root": str(tmp_path),
           "timeout": 6.5, "max_retries": 1, "s2_max_retries": 1,
           "arxiv_delay_seconds": 0, "s2_backoff_base_seconds": 0,
           "user_agent": "Intro2DS offline integration", "max_workers": 2 if variant == "B" else 1,
           "discovery_batch_size": 20 if variant == "A" else 1, "synthetic_demo": True}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(sys, "argv", ["src.main", str(config)])
    main.main()
    run = tmp_path / cfg["run_id"]
    record = json.loads((run / "run.json").read_text())
    assert record["run_status"] == "completed" and record["input_ids"] == IDS
    assert record["synthetic_demo"] is True
    assert all(timeout == 6.5 for _, timeout in calls)
    sdk_calls = [url for url, _ in calls if urlparse(url).path == "/api/query"]
    assert len(sdk_calls) == (1 if variant == "A" else 2)
    for pid, reference_status, schema_status in [(IDS[0], "ok", "ok"), (IDS[1], "failed", "failed")]:
        paper = run / "papers" / pid.replace(".", "-")
        refs = json.loads((paper / "references.json").read_text())
        assert refs["status"] == reference_status
        assert json.loads((paper / "validation.json").read_text())["status"] == schema_status
        manifest = json.loads((paper / "versions/v1/manifest.json").read_text())
        assert manifest["extract_status"] == "ok"
        if pid == IDS[0]:
            assert len(refs["references"]) == 2
        else:
            assert refs["error_code"] == "NOT_FOUND" and refs["references"] == []
    observed = metrics.summarize_request_attempts(metrics.read_events(run / "metrics_events.jsonl"), cfg["run_id"])
    assert observed["observed_request_count"] == 5
    assert observed["observed_attempt_count"] == 6
    assert observed["observed_retry_attempt_count"] == 1
    assert observed["error_code_counts"] == {"HTTP_429": 1, "NOT_FOUND": 1}
    assert observed["observed_attempt_count"] < len(calls)  # library requests remain uninstrumented
