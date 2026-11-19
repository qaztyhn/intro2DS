"""Builders and hand-computed expectations for the TV4 deduplication tests.

Every fixture is a function returning ``list[ParsedDocument]`` for ONE
publication. ``EXPECTED`` holds the element and hierarchy tables worked out by
hand from ``docs/m2/tv4_decisions.md`` (D2, D4, D5, D8), using short ids such as
``"h1"`` that tests expand to ``<paper_key>#h1``. TV2 may reuse these fixtures.

Hierarchy expectation rows are ``(version, element, parent, position)``.
"""

from __future__ import annotations

import itertools

try:  # prefer the team contract once it is merged
    from src.parsing.contracts import ParsedDocument, ParsedNode
except ImportError:  # TEMPORARY copy of Contract §2.3-C, remove when contracts.py lands
    from dataclasses import dataclass, field

    @dataclass
    class ParsedNode:  # type: ignore[no-redef]
        local_id: str
        kind: str
        content: str
        children: list["ParsedNode"] = field(default_factory=list)

    @dataclass
    class ParsedDocument:  # type: ignore[no-redef]
        arxiv_id: str
        version: int
        root: "ParsedNode | None"
        status: str
        warnings: list[str] = field(default_factory=list)


ID = "2310.12345"
OTHER_ID = "2401.00001"
OLD_ID = "hep-th/9901001"
TITLE = "Matching paper"

_counter = itertools.count()


def node(kind: str, content: str, *children: ParsedNode) -> ParsedNode:
    return ParsedNode(f"n{next(_counter):06d}", kind, content, list(children))


def sentence(text: str) -> ParsedNode:
    return node("sentence", text)


def paragraph(*sentences: str) -> ParsedNode:
    """A TV2-style paragraph: content is the text of its sentences."""
    return node("paragraph", " ".join(sentences), *(sentence(s) for s in sentences))


def section(title: str, *children: ParsedNode) -> ParsedNode:
    return node("section", title, *children)


def subsection(title: str, *children: ParsedNode) -> ParsedNode:
    return node("subsection", title, *children)


def root(*children: ParsedNode, title: str = TITLE) -> ParsedNode:
    return node("document", title, *children)


def doc(version: int, tree: ParsedNode | None, *, status: str = "ok",
        arxiv_id: str = ID) -> ParsedDocument:
    return ParsedDocument(arxiv_id, version, tree, status, [])


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def tv4_single_version() -> list[ParsedDocument]:
    return [doc(1, root(section("Introduction",
                                paragraph("We study matching.", "Results are good."))))]


def tv4_two_versions_one_sentence_changed() -> list[ParsedDocument]:  # example (a)
    def tree(last: str):
        return root(section("Introduction", paragraph("We study matching.", last)))
    return [doc(1, tree("Results are good.")), doc(2, tree("Results are strong."))]


def tv4_single_sentence_paragraph() -> list[ParsedDocument]:  # example (b)
    return [doc(1, root(section("Intro", paragraph("We define X."))))]


def tv4_repeated_titles() -> list[ParsedDocument]:  # example (c)
    return [doc(1, root(
        section("Methods", subsection("Results", sentence("A."))),
        section("Analysis", subsection("Results", sentence("B."))),
    ))]


def _list_item(*children: ParsedNode) -> ParsedNode:
    return node("list_item", "", *children)  # TV2 today: label only, usually empty


def tv4_nested_lists() -> list[ParsedDocument]:  # example (d)
    inner = node("list", "", _list_item(paragraph("Inner.")))
    outer = node("list", "", _list_item(paragraph("Outer."), inner))
    return [doc(1, root(section("Lists", outer)))]


def tv4_sibling_list_items() -> list[ParsedDocument]:  # D2 group B, empty siblings
    items = node("list", "", _list_item(paragraph("One.")), _list_item(paragraph("Two.")))
    return [doc(1, root(section("Lists", items)))]


def tv4_three_versions_reordered() -> list[ParsedDocument]:
    def tree(order: tuple[str, ...]):
        return root(section("Introduction", paragraph(*order)))
    return [doc(1, tree(("A.", "B.", "C."))),
            doc(2, tree(("C.", "A.", "B."))),
            doc(3, tree(("B.", "C.", "A.")))]


def tv4_two_publications_same_sentence() -> dict[str, list[ParsedDocument]]:
    def pub(arxiv_id: str):
        return [doc(1, root(section("Introduction", paragraph("Shared sentence."))),
                    arxiv_id=arxiv_id)]
    return {ID: pub(ID), OTHER_ID: pub(OTHER_ID)}


def tv4_no_tex_middle_version() -> list[ParsedDocument]:  # example (e)
    def tree():
        return root(section("Intro", paragraph("Hello.")))
    return [doc(1, tree()), doc(2, None, status="no_tex"), doc(3, tree())]


def tv4_repeated_sentence_same_parent() -> list[ParsedDocument]:  # D7
    return [doc(1, root(section("S", paragraph("Yes.", "Yes."))))]


def tv4_sentence_two_parents() -> list[ParsedDocument]:  # D7, one version
    return [doc(1, root(section("S", paragraph("P1 text.", "Dup."),
                                paragraph("P2 text.", "Dup."))))]


def tv4_status_mix() -> list[ParsedDocument]:  # D8
    def tree():
        return root(section("Intro", paragraph("Hello.")))
    return [doc(1, tree()),
            doc(2, tree(), status="partial"),
            doc(3, None, status="failed"),
            doc(4, None, status="skipped"),
            doc(5, None, status="ok")]  # inconsistent: ok but no root -> treated as failed


def tv4_unknown_kind() -> list[ParsedDocument]:
    return [doc(1, root(node("mystery", "weird", sentence("Hello."))))]


def tv4_old_style_id() -> list[ParsedDocument]:
    return [doc(1, root(section("Intro", paragraph("Hello.")), title="Old paper"),
                arxiv_id=OLD_ID)]


def tv4_self_nested_paragraph() -> list[ParsedDocument]:  # D2 ancestor guard
    return [doc(1, root(node("paragraph", "X", node("paragraph", "X", sentence("X")))))]


def tv4_identical_sibling_headings() -> list[ParsedDocument]:  # D2 group B, index n
    return [doc(1, root(section("A",
                                subsection("Results", sentence("x.")),
                                subsection("Results", sentence("y.")))))]


def tv4_renamed_parent_section() -> list[ParsedDocument]:  # D2 documented cost
    def tree(title: str):
        return root(section(title, subsection("Sub", sentence("S."))))
    return [doc(1, tree("A")), doc(2, tree("A2"))]


# --------------------------------------------------------------------------- #
# Hand-computed expectations (single-publication fixtures)
# --------------------------------------------------------------------------- #

EXPECTED: dict[str, dict] = {
    "tv4_single_version": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "Introduction"),
            "p1": ("paragraph", "We study matching. Results are good."),
            "s1": ("sentence", "We study matching."),
            "s2": ("sentence", "Results are good."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2),
                      (1, "s1", "p1", 3), (1, "s2", "p1", 4)],
    },
    "tv4_two_versions_one_sentence_changed": {
        "elements": {
            "root1": ("document", TITLE),
            "root2": ("document", TITLE),
            "h1": ("section", "Introduction"),
            "p1": ("paragraph", "We study matching. Results are good."),
            "p2": ("paragraph", "We study matching. Results are strong."),
            "s1": ("sentence", "We study matching."),
            "s2": ("sentence", "Results are good."),
            "s3": ("sentence", "Results are strong."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2),
                      (1, "s1", "p1", 3), (1, "s2", "p1", 4),
                      (2, "h1", "root2", 1), (2, "p2", "h1", 2),
                      (2, "s1", "p2", 3), (2, "s3", "p2", 4)],
    },
    "tv4_single_sentence_paragraph": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "Intro"),
            "p1": ("paragraph", "We define X."),
            "s1": ("sentence", "We define X."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3)],
    },
    "tv4_repeated_titles": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "Methods"),
            "h2": ("subsection", "Results"),
            "s1": ("sentence", "A."),
            "h3": ("section", "Analysis"),
            "h4": ("subsection", "Results"),
            "s2": ("sentence", "B."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "h2", "h1", 2), (1, "s1", "h2", 3),
                      (1, "h3", "root1", 4), (1, "h4", "h3", 5), (1, "s2", "h4", 6)],
    },
    "tv4_nested_lists": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "Lists"),
            "l1": ("list", ""),
            "i1": ("list_item", ""),
            "p1": ("paragraph", "Outer."),
            "s1": ("sentence", "Outer."),
            "l2": ("list", ""),
            "i2": ("list_item", ""),
            "p2": ("paragraph", "Inner."),
            "s2": ("sentence", "Inner."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "l1", "h1", 2), (1, "i1", "l1", 3),
                      (1, "p1", "i1", 4), (1, "s1", "p1", 5), (1, "l2", "i1", 6),
                      (1, "i2", "l2", 7), (1, "p2", "i2", 8), (1, "s2", "p2", 9)],
    },
    "tv4_sibling_list_items": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "Lists"),
            "l1": ("list", ""),
            "i1": ("list_item", ""),
            "p1": ("paragraph", "One."),
            "s1": ("sentence", "One."),
            "i2": ("list_item", ""),
            "p2": ("paragraph", "Two."),
            "s2": ("sentence", "Two."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "l1", "h1", 2), (1, "i1", "l1", 3),
                      (1, "p1", "i1", 4), (1, "s1", "p1", 5), (1, "i2", "l1", 6),
                      (1, "p2", "i2", 7), (1, "s2", "p2", 8)],
    },
    "tv4_three_versions_reordered": {
        "elements": {
            "root1": ("document", TITLE),
            "root2": ("document", TITLE),
            "root3": ("document", TITLE),
            "h1": ("section", "Introduction"),
            "p1": ("paragraph", "A. B. C."),
            "p2": ("paragraph", "C. A. B."),
            "p3": ("paragraph", "B. C. A."),
            "s1": ("sentence", "A."),
            "s2": ("sentence", "B."),
            "s3": ("sentence", "C."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3),
                      (1, "s2", "p1", 4), (1, "s3", "p1", 5),
                      (2, "h1", "root2", 1), (2, "p2", "h1", 2), (2, "s3", "p2", 3),
                      (2, "s1", "p2", 4), (2, "s2", "p2", 5),
                      (3, "h1", "root3", 1), (3, "p3", "h1", 2), (3, "s2", "p3", 3),
                      (3, "s3", "p3", 4), (3, "s1", "p3", 5)],
    },
    "tv4_no_tex_middle_version": {
        "elements": {
            "root1": ("document", TITLE),
            "root2": ("document", "Version 2 root"),
            "root3": ("document", TITLE),
            "h1": ("section", "Intro"),
            "p1": ("paragraph", "Hello."),
            "s1": ("sentence", "Hello."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3),
                      (3, "h1", "root3", 1), (3, "p1", "h1", 2), (3, "s1", "p1", 3)],
    },
    "tv4_repeated_sentence_same_parent": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "S"),
            "p1": ("paragraph", "Yes. Yes."),
            "s1": ("sentence", "Yes."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2),
                      (1, "s1", "p1", 3), (1, "s1", "p1", 4)],
    },
    "tv4_sentence_two_parents": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "S"),
            "p1": ("paragraph", "P1 text. Dup."),
            "s1": ("sentence", "P1 text."),
            "s2": ("sentence", "Dup."),
            "p2": ("paragraph", "P2 text. Dup."),
            "s3": ("sentence", "P2 text."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3),
                      (1, "s2", "p1", 4), (1, "p2", "h1", 5), (1, "s3", "p2", 6),
                      (1, "s2", "p2", 7)],
    },
    "tv4_status_mix": {
        "elements": {
            "root1": ("document", TITLE),
            "root2": ("document", TITLE),
            "h1": ("section", "Intro"),
            "p1": ("paragraph", "Hello."),
            "s1": ("sentence", "Hello."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3),
                      (2, "h1", "root2", 1), (2, "p1", "h1", 2), (2, "s1", "p1", 3)],
    },
    "tv4_unknown_kind": {
        "elements": {
            "root1": ("document", TITLE),
            "x1": ("mystery", "weird"),
            "s1": ("sentence", "Hello."),
        },
        "hierarchy": [(1, "x1", "root1", 1), (1, "s1", "x1", 2)],
    },
    "tv4_old_style_id": {
        "elements": {
            "root1": ("document", "Old paper"),
            "h1": ("section", "Intro"),
            "p1": ("paragraph", "Hello."),
            "s1": ("sentence", "Hello."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "p1", "h1", 2), (1, "s1", "p1", 3)],
    },
    "tv4_self_nested_paragraph": {
        "elements": {
            "root1": ("document", TITLE),
            "p1": ("paragraph", "X"),
            "p2": ("paragraph", "X"),
            "s1": ("sentence", "X"),
        },
        "hierarchy": [(1, "p1", "root1", 1), (1, "p2", "p1", 2), (1, "s1", "p2", 3)],
    },
    "tv4_identical_sibling_headings": {
        "elements": {
            "root1": ("document", TITLE),
            "h1": ("section", "A"),
            "h2": ("subsection", "Results"),
            "s1": ("sentence", "x."),
            "h3": ("subsection", "Results"),
            "s2": ("sentence", "y."),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "h2", "h1", 2), (1, "s1", "h2", 3),
                      (1, "h3", "h1", 4), (1, "s2", "h3", 5)],
    },
    "tv4_renamed_parent_section": {
        "elements": {
            "root1": ("document", TITLE),
            "root2": ("document", TITLE),
            "h1": ("section", "A"),
            "h2": ("subsection", "Sub"),
            "s1": ("sentence", "S."),
            "h3": ("section", "A2"),
            "h4": ("subsection", "Sub"),
        },
        "hierarchy": [(1, "h1", "root1", 1), (1, "h2", "h1", 2), (1, "s1", "h2", 3),
                      (2, "h3", "root2", 1), (2, "h4", "h3", 2), (2, "s1", "h4", 3)],
    },
}

# Publication id used by each single-publication fixture (default ``ID``).
FIXTURE_ARXIV_ID: dict[str, str] = {"tv4_old_style_id": OLD_ID}

SINGLE_PUBLICATION_FIXTURES = {name: globals()[name] for name in EXPECTED}
