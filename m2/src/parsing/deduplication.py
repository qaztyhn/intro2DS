"""TV4 — element deduplication across the versions of one publication.

Behaviour is specified by ``m2/docs/decisions/dedup_export.md`` (D1-D10) and locked by
``tests/parsing/test_deduplication.py``. Nothing here normalizes text: ``content``
is compared exactly as TV2 produced it, never fuzzily, and never across
publications.

Identity of a non-root node (D2):

* group A (``sentence``, ``equation``, ``figure``, ``paragraph``) is identified
  by ``(kind, content)``;
* every other kind is identified by ``(kind, content, parent_ref, n)``, where
  ``parent_ref`` is the parent's element id (``None`` when the parent is the
  version root) and ``n`` counts earlier siblings with the same ``(kind,
  content)`` under the same parent.

Roots are never deduplicated: each usable version gets ``<paper_key>#root<N>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from m1.src.utils.common import paper_key

if TYPE_CHECKING:  # contracts.py belongs to the team and may not be merged yet
    from .contracts import ParsedDocument, ParsedNode

# D2: kinds whose ``content`` is the full covered text and therefore identifies them.
CONTENT_KEYED_KINDS = frozenset({"sentence", "equation", "figure", "paragraph"})

# D4: id prefix per kind. Roots use ``ROOT_PREFIX`` + version instead of a counter.
ROOT_KIND = "document"
ROOT_PREFIX = "root"
UNKNOWN_PREFIX = "x"
KIND_PREFIX: dict[str, str] = {
    "part": "h", "chapter": "h", "section": "h", "subsection": "h",
    "subsubsection": "h", "paragraph_heading": "h", "subparagraph_heading": "h",
    "abstract": "b", "appendix": "b", "acknowledgements": "b",
    "paragraph": "p", "sentence": "s", "equation": "eq", "figure": "fig",
    "list": "l", "list_item": "i",
}

# Contract §3.6 statuses.
EXPORTED_STATUSES = frozenset({"ok", "partial"})
KNOWN_STATUSES = frozenset({"ok", "partial", "no_tex", "failed", "skipped"})


class DedupError(Exception):
    """Self-loop, cycle or malformed tree found while deduplicating (D8)."""

    def __init__(self, message: str, arxiv_id: str, version: int,
                 element_id: str | None = None) -> None:
        super().__init__(f"{arxiv_id} v{version}"
                         f"{' ' + element_id if element_id else ''}: {message}")
        self.arxiv_id = arxiv_id
        self.version = version
        self.element_id = element_id


def _assert_acyclic(edges: Iterable[tuple[str, str]], arxiv_id: str, version: int) -> None:
    """Raise :class:`DedupError` on a self-loop or a cycle in ``(child, parent)`` edges.

    Iterative three-colour DFS so very deep versions cannot overflow the stack.
    An element may have several parents, hence the adjacency sets.
    """
    parents: dict[str, set[str]] = {}
    for child, parent in edges:
        if child == parent:
            raise DedupError("self-loop", arxiv_id, version, child)
        parents.setdefault(child, set()).add(parent)
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    for start in sorted(parents):
        if state.get(start) == 2:
            continue
        state[start] = 1
        stack = [(start, iter(sorted(parents.get(start, ()))))]
        while stack:
            current, neighbours = stack[-1]
            for nxt in neighbours:
                if state.get(nxt) == 1:
                    raise DedupError("cycle", arxiv_id, version, nxt)
                if state.get(nxt) is None:
                    state[nxt] = 1
                    stack.append((nxt, iter(sorted(parents.get(nxt, ())))))
                    break
            else:
                state[current] = 2
                stack.pop()


class _Frame:
    """One node whose children are being walked (explicit stack, no recursion)."""

    __slots__ = ("node", "element_id", "children", "seen")

    def __init__(self, node: "ParsedNode", element_id: str | None) -> None:
        self.node = node
        self.element_id = element_id  # None for the version root
        self.children = iter(node.children)
        self.seen: dict[tuple[str, str], int] = {}


class _Publication:
    """Mutable state shared by every version of one publication."""

    def __init__(self, arxiv_id: str) -> None:
        self.arxiv_id = arxiv_id
        self.key = paper_key(arxiv_id)
        self.table: dict[tuple, str] = {}
        self.counters: dict[str, int] = {}
        self.elements: list[dict[str, Any]] = []
        self.hierarchy: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.version_status: dict[int, str] = {}
        self.occurrences_by_version: dict[int, int] = {}
        self._warned_kinds: set[str] = set()

    # -- element creation ------------------------------------------------- #

    def _emit(self, element_id: str, kind: str, content: str) -> str:
        self.elements.append({"element_id": element_id, "arxiv_id": self.arxiv_id,
                              "kind": kind, "content": content})
        return element_id

    def _new_element(self, node: "ParsedNode", version: int) -> str:
        prefix = KIND_PREFIX.get(node.kind)
        if prefix is None:
            prefix = UNKNOWN_PREFIX
            if node.kind not in self._warned_kinds:
                self._warned_kinds.add(node.kind)
                self.warnings.append(f"v{version}: unknown kind {node.kind!r} "
                                     f"given prefix {UNKNOWN_PREFIX!r}")
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return self._emit(f"{self.key}#{prefix}{self.counters[prefix]}",
                          node.kind, node.content)

    def _root(self, version: int, content: str) -> str:
        return self._emit(f"{self.key}#{ROOT_PREFIX}{version}", ROOT_KIND, content)

    def _identify(self, node: "ParsedNode", frame: _Frame, ancestors: set[str],
                  version: int) -> str:
        if node.kind in CONTENT_KEYED_KINDS:
            key: tuple = (node.kind, node.content)
            element_id = self.table.get(key)
            if element_id is None:
                element_id = self.table[key] = self._new_element(node, version)
            elif element_id in ancestors:
                # Equal text nested in itself: reusing would create a cycle (D2).
                self.warnings.append(
                    f"v{version}: {node.kind} {element_id} appears inside itself; "
                    f"created a separate element")
                element_id = self._new_element(node, version)
            return element_id
        same = (node.kind, node.content)
        index = frame.seen.get(same, 0)
        frame.seen[same] = index + 1
        key = (node.kind, node.content, frame.element_id, index)
        element_id = self.table.get(key)
        if element_id is None:
            element_id = self.table[key] = self._new_element(node, version)
        return element_id

    # -- one version ------------------------------------------------------ #

    def add_version(self, doc: "ParsedDocument") -> None:
        version, status, root = doc.version, doc.status, doc.root
        if status not in KNOWN_STATUSES:
            raise ValueError(f"{self.arxiv_id} v{version}: unknown status {status!r}")
        recorded = status
        if status in EXPORTED_STATUSES and root is None:
            recorded = "failed"
            self.warnings.append(f"v{version}: status {status!r} but root is None; "
                                 f"treated as failed")
        elif status == "no_tex" and root is not None:
            recorded = "failed"
            self.warnings.append(f"v{version}: status 'no_tex' but a root was given; "
                                 f"treated as failed")
        self.version_status[version] = recorded
        if recorded == "no_tex":
            # Official M2: an empty/no-TeX version yields no parsed elements.
            # Keep coverage in version_status, not an invented document root.
            return
        if recorded not in EXPORTED_STATUSES:
            return
        if root.kind != ROOT_KIND:
            self.warnings.append(f"v{version}: root kind is {root.kind!r}, "
                                 f"exported as {ROOT_KIND!r}")
        self._walk(version, root)

    def _walk(self, version: int, root: "ParsedNode") -> None:
        if not isinstance(root.content, str):
            raise DedupError("root content is not a string", self.arxiv_id, version)
        root_id = self._root(version, root.content)
        frames = [_Frame(root, None)]
        on_path = {id(root)}
        ancestors: set[str] = set()
        edges: list[tuple[str, str]] = []
        position = 0
        while frames:
            frame = frames[-1]
            child = next(frame.children, None)
            if child is None:
                frames.pop()
                on_path.discard(id(frame.node))
                if frame.element_id is not None:
                    ancestors.discard(frame.element_id)
                continue
            if not isinstance(child.kind, str) or not isinstance(child.content, str):
                raise DedupError(f"node {child.local_id!r} has non-string kind/content",
                                 self.arxiv_id, version)
            if id(child) in on_path:
                raise DedupError(f"node {child.local_id!r} is its own descendant",
                                 self.arxiv_id, version)
            element_id = self._identify(child, frame, ancestors, version)
            position += 1
            parent_id = root_id if frame.element_id is None else frame.element_id
            self.hierarchy.append({"arxiv_id": self.arxiv_id, "version": version,
                                   "element_id": element_id, "parent_id": parent_id,
                                   "position": position})
            edges.append((element_id, parent_id))
            if child.children:
                frames.append(_Frame(child, element_id))
                on_path.add(id(child))
                ancestors.add(element_id)
        self.occurrences_by_version[version] = position
        _assert_acyclic(edges, self.arxiv_id, version)

    # -- statistics ------------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for element in self.elements:
            by_kind[element["kind"]] = by_kind.get(element["kind"], 0) + 1
        parents: dict[tuple[int, str], set[str]] = {}
        for row in self.hierarchy:
            parents.setdefault((row["version"], row["element_id"]), set()).add(row["parent_id"])
        return {
            "total_occurrences": len(self.hierarchy),
            "unique_elements": len({row["element_id"] for row in self.hierarchy}),
            "elements_by_kind": by_kind,
            "multi_parent_count": sum(1 for p in parents.values() if len(p) > 1),
            "occurrences_by_version": dict(self.occurrences_by_version),
            "version_status": dict(self.version_status),
            "warnings": list(self.warnings),
        }


def deduplicate_with_stats(
    documents: list["ParsedDocument"],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Deduplicate every version of ONE publication.

    Returns ``(elements, hierarchy, stats)``. ``elements`` rows have
    ``element_id, arxiv_id, kind, content``; ``hierarchy`` rows have
    ``arxiv_id, version, element_id, parent_id, position`` (``position`` is the
    internal pre-order index, D6, to be kept out of the final 4-column table).
    ``stats`` follows D10; ``version_status`` records every version received.

    Raises ``ValueError`` for documents of different publications, duplicated
    version numbers or an unknown status, and :class:`DedupError` for malformed
    trees. Input documents are never modified.
    """
    arxiv_ids = {doc.arxiv_id for doc in documents}
    if len(arxiv_ids) > 1:
        raise ValueError(f"documents of several publications: {sorted(arxiv_ids)}")
    versions = [doc.version for doc in documents]
    if len(set(versions)) != len(versions):
        raise ValueError("duplicate version numbers in documents")
    publication = _Publication(next(iter(arxiv_ids), ""))
    for doc in sorted(documents, key=lambda d: d.version):
        publication.add_version(doc)
    return publication.elements, publication.hierarchy, publication.stats()


def deduplicate_elements(
    documents: list["ParsedDocument"],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Contract §3.4 signature: ``(elements, hierarchy)`` of one publication."""
    elements, hierarchy, _ = deduplicate_with_stats(documents)
    return elements, hierarchy
