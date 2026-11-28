import pytest
from m1.src.tools.plan_recovery import select_wave,categories


def publication(number,member='m1',status='partial',code='MISSING_BIB_RESOURCE'):
    a=f'2507.{number:05d}'
    return dict(arxiv_id=a,warnings=[] if status=='ok' else [f'{a}v1 stage=gather {code}: main.tex refs'],
        version_status=[dict(version=1,status=status),dict(version=2,status=status)])


def test_stratified_selection_is_order_independent_and_excludes_prior_papers():
    rows=[publication(i) for i in range(1,7)]+[publication(i,status='ok') for i in range(7,10)]
    kwargs=dict(seed=2026,exclude_publications=['2507.00001'],quotas={'bibliography_missing':2,'clean_control':1})
    first,summary=select_wave({'m1':rows},{'m1':'old-run'},**kwargs)
    second,_=select_wave({'m1':list(reversed(rows))},{'m1':'old-run'},**kwargs)
    assert first==second
    assert len({x['arxiv_id'] for x in first})==3
    assert '2507.00001' not in {x['arxiv_id'] for x in first}
    assert summary['m1']['strata']=={'bibliography_missing':2,'clean_control':1}


def test_shortage_is_reported_instead_of_silently_reducing_member_quota():
    with pytest.raises(ValueError,match='only 1 distinct papers'):
        select_wave({'m1':[publication(1)]},{'m1':'old-run'},quotas={'bibliography_missing':2})


def test_other_version_warning_does_not_contaminate_clean_control():
    p=publication(1,status='ok')
    p['warnings']=['2507.00001v2 stage=gather MISSING_DEPENDENCY: main.tex figure.txt']
    assert categories(p,p['version_status'][0])=={'clean_control'}
    assert 'other_text_dependency' in categories(p,p['version_status'][1])
