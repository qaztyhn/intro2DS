from dataclasses import fields, FrozenInstanceError
from pathlib import Path

import pytest

from src.parsing.contracts import SourceVersion, GatheredDocument, ParsedDocument, ParsedNode, BibEntry, BibliographyResult


@pytest.mark.parametrize('model,names', [
    (SourceVersion, ['arxiv_id', 'version', 'source_dir', 'source_files', 'download_status', 'extract_status', 'error_code']),
    (GatheredDocument, ['arxiv_id', 'version', 'root_file', 'latex', 'included_files', 'bib_files', 'status', 'warnings']),
    (ParsedNode, ['local_id', 'kind', 'content', 'children']),
    (ParsedDocument, ['arxiv_id', 'version', 'root', 'status', 'warnings']),
    (BibEntry, ['original_key', 'entry_type', 'fields']),
    (BibliographyResult, ['arxiv_id', 'entries', 'key_mapping', 'warnings']),
])
def test_shared_fields_follow_team_contract(model, names):
    assert [f.name for f in fields(model)] == names


def test_source_version_is_frozen():
    version = SourceVersion('2507.01659', 1, Path('source'), (), 'ok', 'empty', 'NO_TEX_SOURCE')
    with pytest.raises(FrozenInstanceError): version.version = 2
