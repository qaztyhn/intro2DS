"""Publication-scoped orchestration. M1 artifacts and S2 data are never changed."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from .bibliography import cited_keys, extract_bibliography_detailed, unify_bibliography
from .contracts import GatheredDocument, ParsedDocument
from .deduplication import deduplicate_with_stats
from .export import bibtex_rows
from .gathering import gather_source, gather_source_with_root
from .hierarchy import parse_hierarchy
from .m1_adapter import load_versions
from .qa_report import status_row

LOGGER = logging.getLogger(__name__)


@dataclass
class PublicationResult:
    elements: list[dict] = field(default_factory=list)
    hierarchy: list[dict] = field(default_factory=list)
    bibtex: list[dict] = field(default_factory=list)
    version_status: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bib_entries_before: int = 0
    stage_seconds: dict[str, float] = field(default_factory=dict)


def process_publication_detailed(run_dir: Path, arxiv_id: str, *, root_choices=None) -> PublicationResult:
    """Keep recoverable partial versions, and report failed/no-TeX versions in QA.

    Adapter/publication-level errors propagate to the batch runner. Exceptions at
    a version stage are logged and recorded without dropping its sibling versions.
    Bibliography mapping is completed before hierarchy normalization.
    """
    result = PublicationResult()

    def measured(stage, function, *args):
        start = perf_counter()
        try:
            return function(*args)
        finally:
            result.stage_seconds[stage] = result.stage_seconds.get(stage, 0) + perf_counter() - start

    versions = measured('adapter', load_versions, Path(run_dir), arxiv_id)
    if not versions:
        raise ValueError(f'{arxiv_id} version=unknown stage=adapter NO_RECORDED_VERSIONS')
    choices = root_choices or {}
    if set(choices) - {source.version for source in versions}:
        raise ValueError(f'{arxiv_id} stage=root_selection choice refers to an unknown version')
    gathered = []
    for source in versions:
        try:
            if source.version in choices:
                choice = choices[source.version]
                doc = measured('gather', gather_source_with_root, source, choice['root_file'])
                doc.warnings.append(f'{arxiv_id}v{source.version} stage=root_selection reason={choice["reason"]}')
            else:
                doc = measured('gather', gather_source, source)
        except Exception as error:
            LOGGER.exception('%s v%s stage=gather', arxiv_id, source.version)
            doc = GatheredDocument(arxiv_id, source.version, None, '', [], {}, 'failed',
                                   [f'{arxiv_id}v{source.version} stage=gather EXCEPTION: {error}'])
        gathered.append(doc)
    entries, errors, bib_diagnostics = {}, {}, {}
    for doc in gathered:
        if doc.status in {'ok', 'partial'}:
            try:
                entries[doc.version], bib_diagnostics[doc.version] = measured(
                    'bibliography_extract', extract_bibliography_detailed, doc)
            except Exception as error:  # isolation boundary, never silent
                errors[doc.version] = ('bibliography_extract', str(error))
                LOGGER.exception('%s v%s stage=bibliography_extract', arxiv_id, doc.version)
    result.bib_entries_before = sum(map(len, entries.values()))
    bibliography = measured('bibliography_merge', unify_bibliography, arxiv_id, entries)
    result.warnings.extend(bibliography.warnings)
    known_keys = {entry.original_key for entry in bibliography.entries}
    parsed = []
    for source, doc in zip(versions, gathered):
        error = errors.get(doc.version)
        tree = None
        if error is None:
            try:
                tree = measured('hierarchy', parse_hierarchy, doc,
                                bibliography.key_mapping.get(doc.version, {}))
            except Exception as exc:
                error = ('hierarchy', str(exc))
                LOGGER.exception('%s v%s stage=hierarchy', arxiv_id, doc.version)
        if tree is None:
            tree = ParsedDocument(arxiv_id, doc.version, None, 'failed',
                                  list(doc.warnings) + [f'{error[0]}: {error[1]}'])
        missing = set()
        if tree.root is not None:
            pending = [tree.root]
            while pending:
                node = pending.pop()
                missing.update(cited_keys(node.content) - known_keys)
                pending.extend(node.children)
            diagnostics = bib_diagnostics.get(doc.version, [])
            tree.warnings.extend(diagnostics)
            if missing:
                tree.warnings.append(f'{arxiv_id}v{doc.version} stage=bibliography '
                                     'MISSING_CITATION_KEYS: ' + ', '.join(sorted(missing)))
            if (diagnostics or missing) and tree.status == 'ok':
                tree.status = 'partial'
        parsed.append(tree)
        row = status_row(
            doc, tree, has_tex=any(name.lower().endswith('.tex') for name in source.source_files),
            error=error)
        row.update(bibliography_parse_warnings=len(bib_diagnostics.get(doc.version, [])),
                   missing_citation_keys=sorted(missing), root_selection=choices.get(doc.version),
                   missing_bibliography_resources=[w for w in doc.warnings if ' MISSING_BIB_RESOURCE:' in w])
        if row['status'] == 'partial' and (bib_diagnostics.get(doc.version) or missing):
            row.update(stage='bibliography', reason='MISSING_CITATION_KEYS' if missing else 'BIBLIOGRAPHY_WARNINGS')
        result.version_status.append(row)
        result.warnings.extend(tree.warnings)
        LOGGER.info('%s v%s stage=pipeline status=%s warnings=%s',
                    arxiv_id, doc.version, tree.status, tree.warnings)
    result.elements, result.hierarchy, stats = measured('deduplicate', deduplicate_with_stats, parsed)
    result.warnings.extend(stats['warnings'])
    result.bibtex = bibtex_rows(arxiv_id, bibliography.entries)
    return result


def process_publication(run_dir: Path, arxiv_id: str):
    """Contract interface, directly consumable by the benchmark harness."""
    result = process_publication_detailed(run_dir, arxiv_id)
    return result.elements, result.hierarchy, result.bibtex
