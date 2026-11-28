import json

import pytest

from m1.tests.test_extract import seed_version,tar_bytes
from m1.src.schema import paper_directory
from m1.src.stages.extract import extract_paper,extract_version
from m1.src.stages.validation import validate_manifest
from m1.src.tools.audit_sources import audit_run
from m1.src.utils.common import EventLogger,read_json,write_json_atomic
from m2.src.parsing.pipeline import process_publication_detailed

IDENTITY='2507.01659'
MAIN=r'\documentclass{article}\begin{document}\section{Intro}Hello \cite{x}.\input{sections/body.inc}\bibliography{refs}\end{document}'
BBL=r'\begin{thebibliography}{1}\bibitem{x}John Smith.\newblock A Real Work Title.\newblock A Journal, 2020.\end{thebibliography}'


def setup(tmp_path,files):
    cfg,paper,archive=seed_version(tmp_path,content=tar_bytes(files))
    write_json_atomic(paper/'metadata.json',dict(arxiv_id=IDENTITY,status='ok',versions=[1]))
    return cfg,paper,archive


def test_fresh_normal_extract_preserves_converts_audits_and_feeds_m2(tmp_path,capsys):
    cfg,paper,archive=setup(tmp_path,{'main.tex':MAIN.encode(),'main.bbl':BBL.encode(),
        'sections/body.inc':b'Second paragraph. \\input{../passage.txt}',
        'passage.txt':b'Plain text reached by an active input.',
        'custom.weird':b'\\section{Potential source}',
        'figure.png':b'\0binary figure','style.bst':b'ENTRY{title}{}{}'})
    logger=EventLogger(tmp_path/'events.jsonl','offline')
    extract_paper(cfg,IDENTITY,logger)
    version=paper/'versions/v1';manifest=read_json(version/'manifest.json')
    assert archive.is_file()
    assert manifest['source_asset_policy']=='preserve-convert-v1'
    assert not validate_manifest(manifest,arxiv_id=IDENTITY,version=1,source_dir=version/'source')
    for name in ['main.bbl.tex','sections/body.inc.tex','passage.txt.tex']:
        assert name in manifest['source_files']
    assert (version/'original_assets/style.bst').is_file()
    assert not (version/'source/style.bst.tex').exists()
    assert not (version/'source/custom.weird').exists()
    audit=read_json(version/'source_assets_audit.json')
    assert audit['filtered_suffix_counts']=={'.weird':1,'.png':1}
    assert audit['filtered_source_candidates']==['custom.weird']
    assert 'custom.weird' in capsys.readouterr().out
    result=process_publication_detailed(tmp_path/'offline',IDENTITY)
    assert result.version_status[0]['status']=='ok'
    assert not result.version_status[0]['missing_citation_keys']
    assert any('Plain text reached' in r['content'] for r in result.elements)
    report=audit_run(tmp_path/'offline',member_id='24120058')
    assert report['summary']['review_versions']==1
    assert report['retry_plan'][0]['archive']==str(archive.resolve())
    assert report['retry_plan'][0]['paper_dir']==str(paper.resolve())


@pytest.mark.parametrize('bbl,conversion',[
    (r'\entry{x}{article}{}\field{title}{A Real Title}\field{year}{2020}\endentry','biber_to_bib'),
    (r'\bib{x}{article}{title={A Real Title},date={2020},review={MR123456},}','amsrefs_to_bib'),
])
def test_normal_crawler_converts_structured_bbl_to_bib(tmp_path,bbl,conversion):
    cfg,paper,_=setup(tmp_path,{'main.tex':MAIN.replace(r'\input{sections/body.inc}','').encode(),'main.bbl':bbl.encode()})
    manifest=extract_version(cfg,IDENTITY,1)
    assert 'main.bbl.bib' in manifest['source_files']
    audit=read_json(paper/'versions/v1/source_assets_audit.json')
    assert next(r for r in audit['records'] if r['original']=='main.bbl')['conversion']==conversion
    result=process_publication_detailed(tmp_path/'offline',IDENTITY)
    assert result.version_status[0]['status']=='ok'
    assert result.bibtex[0]['title']=='A Real Title'


def test_unknown_bbl_is_retained_and_warns_m2_instead_of_silently_disappearing(tmp_path):
    cfg,paper,_=setup(tmp_path,{'main.tex':MAIN.replace(r'\input{sections/body.inc}','').encode(),
                               'main.bbl':b'unknown bibliography syntax'})
    manifest=extract_version(cfg,IDENTITY,1)
    assert manifest['recovery_bibliography_warnings']
    assert (paper/'versions/v1/original_assets/main.bbl').read_bytes()==b'unknown bibliography syntax'
    result=process_publication_detailed(tmp_path/'offline',IDENTITY)
    assert result.version_status[0]['status']=='partial'
    report=audit_run(tmp_path/'offline')
    assert 'unconverted_source_assets' in report['retry_plan'][0]['review_reasons']


def test_unclosed_biber_record_recovers_next_entry_and_preserves_partial_status(tmp_path):
    main=r'\documentclass{article}\begin{document}\section{Intro}Cites \cite{good}.\bibliography{refs}\end{document}'
    bbl=(r'\entry{bad}{article}{}\field{title}{Broken'+ '\n'+
         r'\entry{good}{article}{}\field{title}{Good Title}\field{year}{2024}\endentry')
    cfg,paper,_=setup(tmp_path,{'main.tex':main.encode(),'main.bbl':bbl.encode()})
    manifest=extract_version(cfg,IDENTITY,1)
    assert any('BBL_TRUNCATED_ENTRY' in w for w in manifest['recovery_bibliography_warnings'])
    assert (paper/'versions/v1/original_assets/main.bbl').read_text(encoding='utf-8')==bbl
    result=process_publication_detailed(tmp_path/'offline',IDENTITY)
    assert result.version_status[0]['status']=='partial'
    assert result.version_status[0]['missing_citation_keys']==[]
    assert len(result.bibtex)==1 and result.bibtex[0]['title']=='Good Title'


def test_pdf_only_archive_is_empty_audited_and_not_a_retry_candidate(tmp_path):
    cfg,paper,archive=seed_version(tmp_path,content=b'%PDF-1.4\nfixture')
    write_json_atomic(paper/'metadata.json',dict(arxiv_id=IDENTITY,status='ok',versions=[1]))
    manifest=extract_version(cfg,IDENTITY,1)
    assert manifest['extract_status']=='empty' and archive.is_file()
    report=audit_run(tmp_path/'offline')
    assert report['summary']['filtered_suffix_counts']=={'.pdf':1}
    assert report['retry_plan']==[]


def test_completed_legacy_source_is_not_rewritten_or_reported_as_preserved(tmp_path,capsys):
    cfg,paper,_=setup(tmp_path,{'main.tex':MAIN.encode()})
    cfg['preserve_source_assets']=False
    manifest=extract_version(cfg,IDENTITY,1)
    cfg['preserve_source_assets']=True
    extract_paper(cfg,IDENTITY,EventLogger(tmp_path/'events.jsonl','offline'))
    assert read_json(paper/'versions/v1/manifest.json')==manifest
    assert 'SOURCE_POLICY_NOT_APPLIED' in capsys.readouterr().out
    assert audit_run(tmp_path/'offline')['retry_plan'][0]['review_reasons']==['source_policy_not_applied']


def test_source_audit_partial_scan_survives_malformed_archive(tmp_path):
    cfg,paper,_=setup(tmp_path,{'../unsafe.tex':b'unsafe'})
    manifest=extract_version(cfg,IDENTITY,1)
    assert manifest['extract_status']=='failed'
    audit=read_json(paper/'versions/v1/source_assets_audit.json')
    assert not audit['archive_scan_complete']
    assert not (paper/'versions/v1/source').exists()


def test_source_preservation_config_rejects_non_boolean(tmp_path):
    import yaml
    from m1.src.config import ConfigError,load_config
    cfg=dict(run_id='fresh',config_name='B',schema_version=1,ids_file='configs/ids.txt',
        output_root='experiments',arxiv_delay_seconds=3,user_agent='fixture',timeout=30,max_retries=3,
        preserve_source_assets='false')
    path=tmp_path/'config.yaml';path.write_text(yaml.safe_dump(cfg),encoding='utf-8')
    with pytest.raises(ConfigError,match='must be a boolean'):load_config(path)


def test_preserved_cache_requires_its_audit_and_keeps_existing_source(tmp_path):
    from m1.src.utils.errors import CrawlError
    cfg,paper,_=setup(tmp_path,{'main.tex':MAIN.encode()})
    extract_version(cfg,IDENTITY,1)
    source=paper/'versions/v1/source/main.tex';old=source.read_bytes()
    (paper/'versions/v1/source_assets_audit.json').unlink()
    with pytest.raises(CrawlError,match='cached source is inconsistent'):
        extract_version(cfg,IDENTITY,1)
    assert source.read_bytes()==old


def test_member_recrawl_configs_are_fresh_and_cover_original_ids():
    from pathlib import Path
    from m1.src.config import load_config
    from m1.src.utils.common import load_ids
    for member in ['24120027','24120046','24120058','24120085']:
        cfg=load_config(Path(f'm1/configs/team/preserve_{member}.yaml'))
        assert cfg['preserve_source_assets'] is True
        assert cfg['run_id'].startswith(f'M1_preserve_{member}_')
        assert len(load_ids(cfg['ids_file']))==5000
        assert cfg['output_root'].name=='recrawl_20261128'


def test_inventory_records_malformed_manifest_without_losing_other_versions(tmp_path):
    cfg,paper,_=setup(tmp_path,{'main.tex':MAIN.encode()})
    (paper/'versions/v1/manifest.json').write_text('[]',encoding='utf-8')
    report=audit_run(tmp_path/'offline')
    assert report['summary']['versions']==1
    assert 'missing_or_invalid_source_audit' in report['retry_plan'][0]['review_reasons']
    assert report['versions'][0]['audit_error']=='manifest must be an object'
