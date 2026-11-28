import io
import urllib.error
import pytest
from m1.src.tools import recover_sources as module


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers={'Content-Length':str(len(body) if length is None else length)}


def setup_network(monkeypatch, responses):
    pending=iter(responses);calls=[]
    def open_request(request,timeout):
        calls.append(request.full_url)
        result=next(pending)
        if isinstance(result,Exception):raise result
        return result
    monkeypatch.setattr(module.urllib.request,'urlopen',open_request)
    monkeypatch.setattr(module.DOWNLOAD_THROTTLE,'wait',lambda:None)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    return calls


def test_transient_and_truncated_downloads_do_not_publish_partial_archive(tmp_path,monkeypatch):
    calls=setup_network(monkeypatch,[urllib.error.URLError('transient'),Response(b'bad',20),Response(b'complete')])
    dest=tmp_path/'source.archive'
    record=module.download_original_archive('https://export.arxiv.org/src/2507.00001v1',dest)
    assert len(calls)==3 and record['attempts']==3
    assert dest.read_bytes()==b'complete'
    assert (tmp_path/'source.archive.partial-2').read_bytes()==b'bad'


def test_existing_archive_is_never_overwritten(tmp_path,monkeypatch):
    calls=setup_network(monkeypatch,[])
    dest=tmp_path/'source.archive';dest.write_bytes(b'original')
    with pytest.raises(FileExistsError):module.download_original_archive('https://example.org',dest)
    assert not calls and dest.read_bytes()==b'original'


def test_permanent_http_failure_does_not_retry(tmp_path,monkeypatch):
    error=urllib.error.HTTPError('https://example.org',404,'not found',{},None)
    calls=setup_network(monkeypatch,[error])
    with pytest.raises(urllib.error.HTTPError):module.download_original_archive('https://example.org',tmp_path/'source.archive')
    assert len(calls)==1 and not (tmp_path/'source.archive').exists()


def test_oversized_response_is_not_published(tmp_path,monkeypatch):
    setup_network(monkeypatch,[Response(b'x',257*1024*1024)])
    with pytest.raises(ValueError,match='256 MiB'):
        module.download_original_archive('https://example.org',tmp_path/'source.archive')
    assert not (tmp_path/'source.archive').exists()
