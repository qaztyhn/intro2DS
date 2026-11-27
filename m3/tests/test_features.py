from pathlib import Path
import csv

import pyarrow.parquet as pq
import pytest

from m3.src.contracts import MaskConfig, make_table, validate_pairs
from m3.src.features import (PAIR_SCHEMA, build_pair_batch, extract_entry_arxiv_ids,
                             get_first_author_surname, is_masked, normalize_title,
                             prepare_pairs)
from m3.src.pairing import read_ids

P, R1, R2 = '2507.00001', '2301.00001', '2301.00002'


def tables():
    bib = make_table('bibtex', [
        {'arxiv_id': P, 'citation_key': 'q1', 'title': r'{Deep} Learning for Images',
         'authors': ['Smith, John', 'Brown, Jane'], 'year': 2023,
         'url': 'https://arxiv.org/abs/2301.00001v2'},
        {'arxiv_id': P, 'citation_key': 'q2'}])
    refs = make_table('references', [
        {'arxiv_id': P, 'ref_arxiv_id': R1, 'ref_title': 'Deep learning for images.',
         'ref_authors': ['John Smith', 'Jane Brown'], 'ref_submission_date': '2023-01-01'},
        {'arxiv_id': P, 'ref_arxiv_id': R2, 'ref_title': 'Quantum computing',
         'ref_authors': ['Jane Doe'], 'ref_submission_date': '2021-01-01'}])
    return bib, refs


def test_cleaning():
    assert normalize_title(r'{Deep} \textbf{Learning}: an Introduction!') == 'deep learning an introduction'
    assert normalize_title(r"Garc\'{i}a") == 'garcia'
    assert get_first_author_surname(['Smith, John']) == 'smith'
    assert get_first_author_surname(['John Smith']) == 'smith'
    assert extract_entry_arxiv_ids({'arxiv_id': P, 'doi': '10.48550/arXiv.2301.00001'}) == {R1}
    assert extract_entry_arxiv_ids({'arxiv_id': P, 'doi': '10.1000/2301.00001'}) == set()
    assert extract_entry_arxiv_ids({'arxiv_id': P, 'journal': 'arXiv:hep-th/9901001v2'}) == {'hep-th/9901001'}


def test_pairs_and_missing():
    bib, refs = tables()
    result = build_pair_batch(arxiv_id=P, bibtex=bib, references=refs)
    validate_pairs(result.queries, refs, result.pairs)
    assert result.queries.num_rows == 2 and result.pairs.num_rows == 4
    rows = result.pairs.to_pylist()
    assert rows[0]['title_similarity'] == 1.0
    assert rows[0]['first_author_match'] is True
    assert rows[0]['year_diff'] == 0 and rows[0]['arxiv_id_match'] is True
    assert rows[0]['author_overlap'] == 1.0
    assert rows[1]['year_diff'] == 2 and rows[1]['arxiv_id_match'] is False
    assert rows[2]['title_similarity'] is None and rows[2]['title_missing'] is True
    assert rows[2]['arxiv_id_match'] is None
    empty = build_pair_batch(arxiv_id=P, bibtex=bib, references=make_table('references', []))
    assert empty.queries.num_rows == 2 and empty.pairs.num_rows == 0
    assert empty.pairs.schema.equals(PAIR_SCHEMA)
    assert build_pair_batch(arxiv_id=P, bibtex=bib, references=refs).pairs.equals(result.pairs)


def test_normalized_candidates_and_old_id():
    bib, refs = tables()
    duplicate = make_table('references', refs.to_pylist() + [{'arxiv_id': P, 'ref_arxiv_id': 'arXiv:2301.00001v2'}])
    result = build_pair_batch(arxiv_id=P, bibtex=bib, references=duplicate)
    assert result.pairs.num_rows == 4 and result.pairs.to_pylist()[0]['title_similarity'] == 1.0
    old = 'hep-th/9901001'
    result = build_pair_batch(arxiv_id=old, bibtex=make_table('bibtex',[{'arxiv_id':old,'citation_key':'k'}]),
                              references=make_table('references',[{'arxiv_id':old,'ref_arxiv_id':'hep-th/9901002v2'}]))
    assert result.pairs.to_pylist()[0]['ref_arxiv_id'] == 'hep-th/9901002'


def test_foreign_and_duplicate_query():
    bib, refs = tables()
    foreign = make_table('references', refs.to_pylist() + [{'arxiv_id':'2507.00002','ref_arxiv_id':R1}])
    with pytest.raises(ValueError, match='expected'):
        build_pair_batch(arxiv_id=P, bibtex=bib, references=foreign)
    with pytest.raises(ValueError, match='trùng'):
        build_pair_batch(arxiv_id=P, bibtex=make_table('bibtex', bib.to_pylist()*2), references=refs)


def test_mask():
    bib, refs = tables()
    before = bib.to_pylist()
    original = build_pair_batch(arxiv_id=P, bibtex=bib, references=refs).pairs
    masked = build_pair_batch(arxiv_id=P, bibtex=bib, references=refs, mask=MaskConfig(fraction=1)).pairs
    assert masked['arxiv_id_match'].null_count == 4
    for name in original.schema.names:
        if name != 'arxiv_id_match':
            assert original[name].equals(masked[name])
    assert bib.to_pylist() == before
    mask = MaskConfig(fraction=.5, seed=2026)
    selected = [is_masked(P, str(k), mask) for k in range(2000)]
    assert .45 < sum(selected)/len(selected) < .55
    assert selected == [is_masked(P, str(k), mask) for k in range(2000)]
    with pytest.raises(ValueError):
        build_pair_batch(arxiv_id=P, bibtex=bib, references=refs, mask=MaskConfig(fraction=2))


def test_prepare_pairs(tmp_path):
    bib, refs = tables()
    empty_paper = '2507.00002'
    bib = make_table('bibtex', bib.to_pylist() + [{'arxiv_id':empty_paper,'citation_key':'q3'}])
    catalog = tmp_path/'catalog'; catalog.mkdir()
    pq.write_table(bib, tmp_path/'bibtex.parquet')
    for name, table in {
        'papers': make_table('papers',[{'arxiv_id':a} for a in (P,empty_paper)]),
        'references': refs,
        'queries': make_table('queries',[{'arxiv_id':r['arxiv_id'],'citation_key':r['citation_key']} for r in bib.to_pylist()]),
        'crawl_status': make_table('crawl_status',[{'arxiv_id':P,'status':'ok','candidate_count':2},
                                                 {'arxiv_id':empty_paper,'status':'empty','candidate_count':0}])}.items():
        pq.write_table(table,catalog/f'{name}.parquet')
    kwargs = dict(catalog_dir=catalog,bibtex_path=tmp_path/'bibtex.parquet',publication_ids=[empty_paper,P])
    out = prepare_pairs(**kwargs,output_dir=tmp_path/'out')
    assert pq.read_table(out/'pairs.parquet').num_rows == 4
    with (out/'pairing_report.csv').open() as f:
        report = list(csv.DictReader(f))
    assert report[0]['eligible'] == 'True' and report[1]['reason'] == 'no_candidates'
    with pytest.raises(FileExistsError):
        prepare_pairs(**kwargs,output_dir=out)
    with pytest.raises(ValueError,match='absent'):
        prepare_pairs(**{**kwargs,'publication_ids':['2507.99999']},output_dir=tmp_path/'bad')
    # Catalog/M2 key drift is fatal.
    pq.write_table(make_table('queries',[{'arxiv_id':P,'citation_key':'other'}]),catalog/'queries.parquet')
    with pytest.raises(ValueError,match='snapshot'):
        prepare_pairs(**kwargs,output_dir=tmp_path/'bad2')
    assert not (tmp_path/'bad2').exists()


def test_read_ids(tmp_path):
    p=tmp_path/'ids.txt'; p.write_text(P+'\n'+R1+'\n')
    assert read_ids(p)==[P,R1]
    p.write_text(P+'\n'+P+'v2\n')
    with pytest.raises(ValueError,match='duplicate'):
        read_ids(p)
