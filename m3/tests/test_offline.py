"""T3-6: chuỗi pairs -> predictions chạy được khi mạng bị chặn; module của TV3 không import thư viện mạng/NLP cấm (D9)."""
import ast
import socket
from pathlib import Path

import pytest

from m3.src.evaluate import evaluate_mrr_at_5
from m3.src.model import fit_ranker, predict_top5
from m3.tests.test_model import make_synth

SRC = Path(__file__).resolve().parents[1] / "src"
TV3_MODULES = ["model.py", "evaluate.py"]          # phase 4 thêm experiments.py
BANNED = ("requests", "urllib.request", "huggingface_hub", "transformers", "sentence_transformers", "nltk", "spacy",
          "urllib3", "http.client", "socket", "ftplib", "rapidfuzz")


def _imported(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            found.add(base)
            found |= {f"{base}.{a.name}" for a in node.names}
    return found


@pytest.mark.parametrize("name", TV3_MODULES)
def test_no_banned_imports(name):
    imported = _imported(SRC / name)
    hits = sorted(i for i in imported for b in BANNED if i == b or i.startswith(b + "."))
    assert not hits, f"{name} import module bị cấm: {hits}"


def test_static_check_detects_banned_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import requests\nfrom urllib import request\nfrom urllib.request import urlopen\n")
    got = _imported(bad)
    assert {"requests", "urllib.request"} <= got            # `from urllib import request` -> 'urllib.request'


@pytest.mark.parametrize("estimator,params", [("logreg", None),
                                              ("lgbm_ranker", {"n_estimators": 5, "num_leaves": 4, "min_child_samples": 2})])
def test_pipeline_runs_with_network_blocked(monkeypatch, estimator, params):
    def blocked(*a, **k):
        raise AssertionError("truy cập mạng bị chặn trong test offline")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    with pytest.raises(AssertionError):                     # xác nhận chặn thật sự có hiệu lực
        socket.socket()
    d = make_synth(n_train=14)
    model = fit_ranker(pairs=d["pairs"], labels=d["labels"], splits=d["splits"], seed=2026,
                       estimator=estimator, params=params)
    out = predict_top5(model, queries=d["queries"], pairs=d["pairs"], labels=d["labels"], splits=d["splits"])
    assert out.num_rows == d["queries"].num_rows
    evaluate_mrr_at_5(predictions=out, labels=d["labels"], splits=d["splits"])
