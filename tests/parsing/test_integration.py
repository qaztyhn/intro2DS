from src.parsing.m1_adapter import load_versions
from src.parsing.gathering import gather_source


def test_adapter_to_gathering_all_versions_and_no_tex(corpus):
    cfg, identity, paper = corpus  # Synthetic offline M1 fixture, not the real corpus.
    before = {p: p.read_bytes() for p in paper.rglob('*') if p.is_file()}
    versions = load_versions(cfg['output_root'] / cfg['run_id'], identity)
    documents = [gather_source(version) for version in versions]
    assert [doc.version for doc in documents] == [1, 2]
    assert [doc.status for doc in documents] == ['ok', 'no_tex']
    assert documents[0].root_file == 'main.tex'
    assert documents[1].root_file is None and documents[1].latex == ''
    assert all(p.read_bytes() == value for p, value in before.items())
