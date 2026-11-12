"""Shared internal contracts for M2 (provisional copy)."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SourceVersion:
    arxiv_id: str
    version: int
    source_dir: Path
    source_files: tuple[str, ...]
    download_status: str
    extract_status: str
    error_code: str | None = None


@dataclass
class GatheredDocument:
    arxiv_id: str
    version: int
    root_file: str | None
    latex: str
    included_files: list[str]
    bib_files: dict[str, str]
    status: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedNode:
    local_id: str
    kind: str
    content: str
    children: list["ParsedNode"] = field(default_factory=list)


@dataclass
class ParsedDocument:
    arxiv_id: str
    version: int
    root: ParsedNode | None
    status: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class BibEntry:
    original_key: str
    entry_type: str
    fields: dict[str, object]


@dataclass
class BibliographyResult:
    arxiv_id: str
    entries: list[BibEntry]
    key_mapping: dict[int, dict[str, str]]
    warnings: list[str] = field(default_factory=list)