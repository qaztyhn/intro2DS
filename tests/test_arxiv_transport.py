"""Exercise actual arxiv.Client -> Requests adapter without network I/O."""

from unittest.mock import Mock

import arxiv
import pytest
import requests

from src.discovery import arxiv_api


FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
<id>synthetic</id><title>Offline transport fixture</title><updated>2025-01-01T00:00:00Z</updated>
<opensearch:totalResults>0</opensearch:totalResults><opensearch:startIndex>0</opensearch:startIndex>
<opensearch:itemsPerPage>0</opensearch:itemsPerPage></feed>'''


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result._content = FEED
    result.headers = requests.structures.CaseInsensitiveDict()
    return result


@pytest.fixture
def cfg():
    return {"timeout": 7.5, "max_retries": 2, "arxiv_delay_seconds": 3,
            "user_agent": "Intro2DS offline transport test"}


def test_yaml_timeout_reaches_real_http_adapter(tmp_path, cfg):
    import yaml
    from src.config import load_config
    ids = tmp_path / "ids.txt"
    ids.write_text("2505.16300\n")
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump(dict(cfg, run_id="transport", config_name="B", schema_version=1,
                                        ids_file=str(ids), output_root=str(tmp_path))))
    loaded = load_config(source)
    client, opts, throttle = arxiv_api.make_clients(loaded)
    adapter = client._session.get_adapter("https://")
    adapter.send = Mock(return_value=response())
    list(client.results(arxiv.Search(id_list=["2505.16300"], max_results=1)))
    assert adapter.send.call_count == 1
    assert adapter.send.call_args.kwargs["timeout"] == loaded["timeout"] == 7.5
    assert opts["timeout"] == 7.5
    assert (client.page_size, client.delay_seconds, client.num_retries) == (100, 3, 2)
    assert throttle.min_interval == 3


def test_each_library_retry_uses_timeout_without_extra_transport_retries(cfg):
    cfg.update(max_retries=1, arxiv_delay_seconds=0)
    client, _, _ = arxiv_api.make_clients(cfg)
    adapter = client._session.get_adapter("https://")
    assert adapter.max_retries.total == 0
    adapter.send = Mock(side_effect=[response(503), response(200)])
    list(client.results(arxiv.Search(id_list=["2505.16300"], max_results=1)))
    assert adapter.send.call_count == 2
    assert [call.kwargs["timeout"] for call in adapter.send.call_args_list] == [7.5, 7.5]


@pytest.mark.parametrize("error", [requests.ReadTimeout, requests.ConnectTimeout])
def test_transport_timeout_uses_existing_library_retry_policy(cfg, error):
    cfg.update(max_retries=1, arxiv_delay_seconds=0)
    client, _, _ = arxiv_api.make_clients(cfg)
    adapter = client._session.get_adapter("https://")
    adapter.send = Mock(side_effect=error("SYNTHETIC timeout"))
    with pytest.raises(error):
        list(client.results(arxiv.Search(id_list=["2505.16300"], max_results=1)))
    # ConnectTimeout is also ConnectionError, which arxiv already retries.
    # ReadTimeout is not ConnectionError and escapes immediately in arxiv 4.x.
    assert adapter.send.call_count == (2 if issubclass(error, requests.ConnectionError) else 1)
    assert all(call.kwargs["timeout"] == 7.5 for call in adapter.send.call_args_list)


def test_two_clients_and_plain_requests_session_remain_independent(cfg):
    plain_send = requests.Session.send
    plain_request = requests.Session.request
    one, _, _ = arxiv_api.make_clients(dict(cfg, timeout=4))
    two, _, _ = arxiv_api.make_clients(dict(cfg, timeout=9))
    for client, timeout in [(one, 4), (two, 9)]:
        adapter = client._session.get_adapter("https://")
        adapter.send = Mock(return_value=response())
        client._session.get("https://example.invalid")
        assert adapter.send.call_args.kwargs["timeout"] == timeout
    plain = requests.Session()
    adapter = plain.get_adapter("https://")
    adapter.send = Mock(return_value=response())
    plain.get("https://example.invalid")
    assert adapter.send.call_args.kwargs["timeout"] is None
    assert requests.Session.send is plain_send
    assert requests.Session.request is plain_request


@pytest.mark.parametrize("timeout", [0, -1, True, None, "30", float("inf"), float("nan")])
def test_invalid_timeout_rejected_before_transport(cfg, timeout):
    with pytest.raises((ValueError, TypeError)):
        arxiv_api.make_clients(dict(cfg, timeout=timeout))


def test_explicit_request_timeout_is_preserved_and_redirects_keep_default(cfg):
    client, _, _ = arxiv_api.make_clients(cfg)
    adapter = client._session.get_adapter("https://")
    redirect = response(302)
    redirect.headers["Location"] = "https://example.invalid/final"
    replies = iter([redirect, response(200), response(200)])
    def send(prepared, **kwargs):
        result = next(replies)
        result.request, result.url = prepared, prepared.url
        return result
    adapter.send = Mock(side_effect=send)
    client._session.get("https://example.invalid/redirect")
    client._session.get("https://example.invalid/custom", timeout=(2, 5))
    assert [call.kwargs["timeout"] for call in adapter.send.call_args_list] == [7.5, 7.5, (2, 5)]


def test_unexpected_library_transport_fails_without_replacing_it():
    from types import SimpleNamespace
    from src.utils.arxiv_transport import configure_arxiv_timeout
    original = object()
    client = SimpleNamespace(_session=original)
    with pytest.raises(RuntimeError, match="Unsupported arxiv.Client transport"):
        configure_arxiv_timeout(client, 10)
    assert client._session is original
