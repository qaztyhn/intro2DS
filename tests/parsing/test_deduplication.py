"""Invariant tests for TV4 element deduplication (docs/m2/tv4_decisions.md).

Written before the implementation (Phase 2): every test is expected to fail with
NotImplementedError until Phase 3. Expected tables are hand-computed in
``fixtures/tv4_builders.py``.
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import tv4_builders as fx  # noqa: E402

from src.parsing.deduplication import (  # noqa: E402
    deduplicate_elements, deduplicate_with_stats)
from src.utils.common import paper_key  # noqa: E402

SINGLE = sorted(fx.SINGLE_PUBLICATION_FIXTURES)


# ----------------------------------------------------------------- helpers --

def build(name):
    return fx.SINGLE_PUBLICATION_FIXTURES[name]()


def arxiv_of(name):
    return fx.FIXTURE_ARXIV_ID.get(name, fx.ID)


def full(name, short):
    return f"{paper_key(arxiv_of(name))}#{short}"


def run(docs):
    return deduplicate_elements(docs)


def edges_by_version(hierarchy):
    """{(arxiv_id, version): [(element_id, parent_id), ...]} in input order."""
    out = {}
    for row in hierarchy:
        out.setdefault((row["arxiv_id"], row["version"]), []).append(
            (row["element_id"], row["parent_id"]))
    return out


def has_cycle(edges):
    """Iterative DFS over child -> parent edges (self-loops count as cycles)."""
    parents = {}
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)
    state = {}
    for start in parents:
        if state.get(start) == 2:
            continue
        stack = [(start, iter(parents.get(start, ())))]
        state[start] = 1
        while stack:
            current, it = stack[-1]
            for nxt in it:
                if state.get(nxt) == 1:
                    return True
                if state.get(nxt) is None:
                    state[nxt] = 1
                    stack.append((nxt, iter(parents.get(nxt, ()))))
                    break
            else:
                state[current] = 2
                stack.pop()
    return False


def preorder(tree):
    """(kind, content) of every non-root node in DFS pre-order."""
    out = []
    stack = list(reversed(tree.children))
    while stack:
        n = stack.pop()
        out.append((n.kind, n.content))
        stack.extend(reversed(n.children))
    return out


# ------------------------------------------- hand-computed tables (all) -----

@pytest.mark.parametrize("name", SINGLE)
def test_tables_match_hand_computed_expectation(name):
    elements, hierarchy = run(build(name))
    exp = fx.EXPECTED[name]
    got_elements = {e["element_id"]: (e["kind"], e["content"]) for e in elements}
    assert got_elements == {full(name, k): v for k, v in exp["elements"].items()}
    assert len(elements) == len(exp["elements"])
    got_rows = sorted((r["version"], r["element_id"], r["parent_id"], r["position"])
                      for r in hierarchy)
    want_rows = sorted((v, full(name, e), full(name, p), pos)
                       for v, e, p, pos in exp["hierarchy"])
    assert got_rows == want_rows


# ------------------------------------------------------ identifiers (D5) ----

@pytest.mark.parametrize("name", SINGLE)
def test_element_ids_are_unique(name):
    elements, _ = run(build(name))
    ids = [e["element_id"] for e in elements]
    assert len(ids) == len(set(ids)) and ids


@pytest.mark.parametrize("name", SINGLE)
def test_element_id_has_paper_key_prefix_and_matching_arxiv_id(name):
    elements, hierarchy = run(build(name))
    prefix = paper_key(arxiv_of(name)) + "#"
    assert all(e["element_id"].startswith(prefix) for e in elements)
    assert all(e["arxiv_id"] == arxiv_of(name) for e in elements)
    assert all(r["arxiv_id"] == arxiv_of(name) for r in hierarchy)
    assert all(r["element_id"].startswith(prefix) and r["parent_id"].startswith(prefix)
               for r in hierarchy)


def test_old_style_arxiv_id_uses_double_underscore_key():
    elements, _ = run(build("tv4_old_style_id"))
    assert all(e["element_id"].startswith("hep-th__9901001#") for e in elements)


# ------------------------------------------------ dedup across versions -----

def test_unchanged_sentence_keeps_one_element_id_across_versions():
    elements, hierarchy = run(build("tv4_two_versions_one_sentence_changed"))
    kept = full("tv4_two_versions_one_sentence_changed", "s1")
    versions_using = {r["version"] for r in hierarchy if r["element_id"] == kept}
    assert versions_using == {1, 2}
    assert [e["content"] for e in elements if e["element_id"] == kept] == ["We study matching."]


def test_changed_sentence_creates_a_new_element():
    name = "tv4_two_versions_one_sentence_changed"
    elements, _ = run(build(name))
    contents = [e["content"] for e in elements if e["kind"] == "sentence"]
    assert sorted(contents) == ["Results are good.", "Results are strong.", "We study matching."]
    assert len(elements) == 8  # hand count: 2 roots + 1 section + 2 paragraphs + 3 sentences


def test_reordered_sentences_reuse_ids_but_change_paragraph():
    name = "tv4_three_versions_reordered"
    elements, hierarchy = run(build(name))
    assert len([e for e in elements if e["kind"] == "sentence"]) == 3
    assert len([e for e in elements if e["kind"] == "paragraph"]) == 3
    by_version = {}
    for r in sorted(hierarchy, key=lambda r: (r["version"], r["position"])):
        if r["element_id"].split("#")[1].startswith("s"):
            by_version.setdefault(r["version"], []).append(r["element_id"].split("#")[1])
    assert by_version == {1: ["s1", "s2", "s3"], 2: ["s3", "s1", "s2"], 3: ["s2", "s3", "s1"]}


# ------------------------------------- kind in the key / ancestor guard -----

def test_kind_is_part_of_the_dedup_key():
    """Example (b): paragraph and sentence share content but never share an id."""
    name = "tv4_single_sentence_paragraph"
    elements, hierarchy = run(build(name))
    by_kind = {e["kind"]: e["element_id"] for e in elements}
    assert by_kind["paragraph"] != by_kind["sentence"]
    assert all(r["element_id"] != r["parent_id"] for r in hierarchy)


def test_headings_with_same_title_under_different_parents_stay_separate():
    """Example (c): each 'Results' keeps exactly its own child."""
    name = "tv4_repeated_titles"
    elements, hierarchy = run(build(name))
    results = [e["element_id"] for e in elements if e["content"] == "Results"]
    assert len(results) == 2
    for rid in results:
        children = [r["element_id"] for r in hierarchy if r["parent_id"] == rid]
        assert len(children) == 1
    parents = {r["element_id"]: r["parent_id"] for r in hierarchy if r["element_id"] in results}
    assert len(set(parents.values())) == 2


def test_identical_sibling_headings_are_distinguished_by_index():
    elements, _ = run(build("tv4_identical_sibling_headings"))
    assert len([e for e in elements if e["content"] == "Results"]) == 2


def test_renamed_parent_makes_child_heading_a_new_element():
    """D2 documented cost: same subsection title under a renamed section."""
    elements, _ = run(build("tv4_renamed_parent_section"))
    assert len([e for e in elements if e["content"] == "Sub"]) == 2
    assert len([e for e in elements if e["content"] == "S."]) == 1


def test_nested_empty_lists_do_not_collapse_or_loop():
    """Example (d): empty list/list_item nodes must not merge into a cycle."""
    elements, hierarchy = run(build("tv4_nested_lists"))
    assert len([e for e in elements if e["kind"] == "list"]) == 2
    assert len([e for e in elements if e["kind"] == "list_item"]) == 2
    assert not has_cycle([(r["element_id"], r["parent_id"]) for r in hierarchy])


def test_empty_sibling_list_items_stay_separate_with_their_own_children():
    """Empty list_item nodes of one list must not merge into a single element."""
    name = "tv4_sibling_list_items"
    elements, hierarchy = run(build(name))
    items = [e["element_id"] for e in elements if e["kind"] == "list_item"]
    assert len(items) == 2
    for item in items:
        assert len([r for r in hierarchy if r["parent_id"] == item]) == 1


def test_ancestor_guard_gives_nested_equal_paragraphs_distinct_ids():
    elements, hierarchy = run(build("tv4_self_nested_paragraph"))
    paragraphs = [e["element_id"] for e in elements if e["kind"] == "paragraph"]
    assert len(paragraphs) == 2 and len(set(paragraphs)) == 2
    assert all(r["element_id"] != r["parent_id"] for r in hierarchy)


# ----------------------------------------------------------- roots (D1) -----

@pytest.mark.parametrize("name", SINGLE)
def test_each_version_has_its_own_root_not_listed_as_hierarchy_element(name):
    docs = build(name)
    elements, hierarchy = run(docs)
    roots = {e["element_id"] for e in elements if e["kind"] == "document"}
    prefix = paper_key(arxiv_of(name)) + "#root"
    assert all(r.startswith(prefix) for r in roots)
    hierarchy_elements = {r["element_id"] for r in hierarchy}
    assert not roots & hierarchy_elements
    # every version that emitted rows hangs its top level off exactly one root
    for (_, version), edges in edges_by_version(hierarchy).items():
        top = {p for _, p in edges if p in roots}
        assert top == {f"{prefix}{version}"}


def test_identical_versions_do_not_share_a_root():
    docs = [fx.doc(1, fx.root(fx.section("Intro", fx.paragraph("Same.")))),
            fx.doc(2, fx.root(fx.section("Intro", fx.paragraph("Same."))))]
    elements, _ = run(docs)
    assert len([e for e in elements if e["kind"] == "document"]) == 2
    assert len(elements) == 2 + 3  # roots + section, paragraph, sentence reused


# ------------------------------------- cycles / publications / statuses -----

@pytest.mark.parametrize("name", SINGLE)
def test_no_self_loop_or_cycle_per_version(name):
    _, hierarchy = run(build(name))
    for edges in edges_by_version(hierarchy).values():
        assert all(child != parent for child, parent in edges)
        assert not has_cycle(edges)


def test_no_element_is_shared_between_publications():
    pubs = fx.tv4_two_publications_same_sentence()
    results = {aid: run(docs) for aid, docs in pubs.items()}
    ids = {aid: {e["element_id"] for e in els} for aid, (els, _) in results.items()}
    assert not ids[fx.ID] & ids[fx.OTHER_ID]
    for aid, (elements, hierarchy) in results.items():
        prefix = paper_key(aid) + "#"
        assert all(i.startswith(prefix) for i in ids[aid])
        assert all(r["parent_id"].startswith(prefix) for r in hierarchy)
    # same content and same structure => same short ids, different publications
    short = {aid: sorted(i.split("#")[1] for i in v) for aid, v in ids.items()}
    assert short[fx.ID] == short[fx.OTHER_ID]


def test_documents_of_different_publications_are_rejected():
    pubs = fx.tv4_two_publications_same_sentence()
    mixed = pubs[fx.ID] + pubs[fx.OTHER_ID]
    with pytest.raises(ValueError):
        run(mixed)


def test_versions_without_tex_are_recorded_as_empty_root_only():
    """Example (e)."""
    name = "tv4_no_tex_middle_version"
    elements, hierarchy = run(build(name))
    root2 = full(name, "root2")
    assert [e for e in elements if e["element_id"] == root2][0]["content"] == "Version 2 root"
    assert not [r for r in hierarchy if r["version"] == 2]
    assert not [r for r in hierarchy if r["parent_id"] == root2]


def test_failed_skipped_and_rootless_ok_versions_emit_nothing():
    name = "tv4_status_mix"
    elements, hierarchy = run(build(name))
    roots = sorted(e["element_id"] for e in elements if e["kind"] == "document")
    assert roots == [full(name, "root1"), full(name, "root2")]
    assert {r["version"] for r in hierarchy} == {1, 2}


def test_partial_version_is_exported_like_ok():
    name = "tv4_status_mix"
    _, hierarchy = run(build(name))
    assert len([r for r in hierarchy if r["version"] == 2]) == 3


# ------------------------------------------- determinism / purity (rule 6) --

@pytest.mark.parametrize("name", ["tv4_two_versions_one_sentence_changed",
                                  "tv4_three_versions_reordered", "tv4_no_tex_middle_version"])
def test_two_runs_give_identical_output(name):
    assert run(build(name)) == run(build(name))


@pytest.mark.parametrize("name", ["tv4_two_versions_one_sentence_changed",
                                  "tv4_three_versions_reordered", "tv4_no_tex_middle_version",
                                  "tv4_status_mix"])
def test_input_order_does_not_matter(name):
    docs = build(name)
    assert run(list(reversed(docs))) == run(docs)


def test_input_documents_are_not_mutated():
    docs = build("tv4_two_versions_one_sentence_changed")
    before = copy.deepcopy(docs)
    run(docs)
    assert docs == before


# ---------------------------------------------- order recovery (D6) ---------

@pytest.mark.parametrize("name", SINGLE)
def test_dfs_order_of_each_version_is_recoverable_from_position(name):
    docs = build(name)
    elements, hierarchy = run(docs)
    meta = {e["element_id"]: (e["kind"], e["content"]) for e in elements}
    for d in docs:
        rows = sorted((r for r in hierarchy if r["version"] == d.version),
                      key=lambda r: r["position"])
        if d.root is None or d.status not in ("ok", "partial"):
            assert not rows
            continue
        assert [r["position"] for r in rows] == list(range(1, len(rows) + 1))
        assert [meta[r["element_id"]] for r in rows] == preorder(d.root)


@pytest.mark.parametrize("name", SINGLE)
def test_hierarchy_rows_are_ordered_by_version_then_position(name):
    _, hierarchy = run(build(name))
    keys = [(r["arxiv_id"], r["version"], r["position"]) for r in hierarchy]
    assert keys == sorted(keys)


# --------------------------------- multiple parents / repeated nodes (D7) ---

def test_same_sentence_twice_under_one_parent_keeps_both_rows():
    name = "tv4_repeated_sentence_same_parent"
    _, hierarchy = run(build(name))
    s1 = full(name, "s1")
    rows = [(r["position"]) for r in hierarchy if r["element_id"] == s1]
    assert rows == [3, 4]


def test_sentence_may_have_two_parents_in_one_version():
    name = "tv4_sentence_two_parents"
    _, hierarchy = run(build(name))
    dup = full(name, "s2")
    assert {r["parent_id"] for r in hierarchy if r["element_id"] == dup} == {
        full(name, "p1"), full(name, "p2")}


# ----------------------------------------------- unknown kinds / stats ------

def test_unknown_kind_gets_x_prefix_and_a_warning():
    name = "tv4_unknown_kind"
    elements, _, stats = deduplicate_with_stats(build(name))
    assert full(name, "x1") in {e["element_id"] for e in elements}
    assert any("mystery" in w for w in stats["warnings"])


def test_stats_for_two_versions_one_sentence_changed():
    _, _, stats = deduplicate_with_stats(build("tv4_two_versions_one_sentence_changed"))
    assert stats["total_occurrences"] == 8      # non-root nodes over both versions
    assert stats["unique_elements"] == 6        # non-root elements
    assert stats["elements_by_kind"] == {"document": 2, "section": 1, "paragraph": 2,
                                         "sentence": 3}
    assert stats["multi_parent_count"] == 0
    assert stats["version_status"] == {1: "ok", 2: "ok"}


def test_stats_for_no_tex_version():
    _, _, stats = deduplicate_with_stats(build("tv4_no_tex_middle_version"))
    assert (stats["total_occurrences"], stats["unique_elements"]) == (6, 3)
    assert stats["version_status"] == {1: "ok", 2: "no_tex", 3: "ok"}


def test_stats_record_every_version_status_including_failures():
    _, _, stats = deduplicate_with_stats(build("tv4_status_mix"))
    assert stats["version_status"] == {1: "ok", 2: "partial", 3: "failed",
                                       4: "skipped", 5: "failed"}
    assert any("5" in w for w in stats["warnings"])  # ok-without-root is never silent


def test_stats_count_elements_with_several_parents():
    _, _, stats = deduplicate_with_stats(build("tv4_sentence_two_parents"))
    assert stats["multi_parent_count"] == 1


# --------------------------------- robustness added in Phase 3 (D2, D8) -----

from src.parsing import deduplication as dedup  # noqa: E402


def test_match_is_exact_never_fuzzy():
    """Rule 5: 'A.' and 'A. ' differ, so they are two elements (no re-normalization)."""
    docs = [fx.doc(1, fx.root(fx.section("S", fx.paragraph("A.")))),
            fx.doc(2, fx.root(fx.section("S", fx.paragraph("A. "))))]
    elements, _ = run(docs)
    assert len([e for e in elements if e["kind"] == "sentence"]) == 2


def test_ancestor_guard_leaves_a_warning():
    _, _, stats = deduplicate_with_stats(build("tv4_self_nested_paragraph"))
    assert any("appears inside itself" in w for w in stats["warnings"])


def test_empty_input_gives_empty_output():
    elements, hierarchy, stats = deduplicate_with_stats([])
    assert (elements, hierarchy) == ([], [])
    assert stats["total_occurrences"] == 0 and stats["version_status"] == {}


def test_root_without_children_emits_only_the_root():
    elements, hierarchy = run([fx.doc(1, fx.root())])
    assert [e["element_id"] for e in elements] == [f"{paper_key(fx.ID)}#root1"]
    assert hierarchy == []


def test_duplicate_version_numbers_are_rejected():
    docs = build("tv4_single_version") * 2
    with pytest.raises(ValueError):
        run(docs)


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        run([fx.doc(1, fx.root(), status="weird")])


def test_no_tex_with_a_root_is_treated_as_failed_with_a_warning():
    elements, hierarchy, stats = deduplicate_with_stats(
        [fx.doc(1, fx.root(fx.section("S")), status="no_tex")])
    assert (elements, hierarchy) == ([], [])
    assert stats["version_status"] == {1: "failed"} and stats["warnings"]


def test_very_deep_tree_does_not_overflow_the_stack():
    depth = 5000
    tree = fx.sentence("leaf")
    for i in range(depth):
        tree = fx.node("section", f"s{i}", tree)
    elements, hierarchy = run([fx.doc(1, fx.root(tree))])
    assert len(hierarchy) == depth + 1
    assert [r["position"] for r in hierarchy] == list(range(1, depth + 2))


def test_tree_that_contains_itself_raises_dedup_error():
    a = fx.node("section", "A")
    b = fx.node("section", "B", a)
    a.children.append(b)  # a -> b -> a
    with pytest.raises(dedup.DedupError) as info:
        run([fx.doc(7, fx.root(a))])
    assert info.value.arxiv_id == fx.ID and info.value.version == 7


def test_non_string_content_raises_dedup_error():
    bad = fx.node("sentence", None)
    with pytest.raises(dedup.DedupError):
        run([fx.doc(1, fx.root(bad))])


def test_same_node_object_under_two_parents_is_allowed():
    shared = fx.sentence("Shared.")
    tree = fx.root(fx.section("A", shared), fx.section("B", shared))
    _, hierarchy = run([fx.doc(1, tree)])
    assert len([r for r in hierarchy if r["element_id"].endswith("#s1")]) == 2


@pytest.mark.parametrize("edges", [[("a", "a")], [("a", "b"), ("b", "a")],
                                   [("a", "b"), ("b", "c"), ("c", "a")]])
def test_acyclic_check_rejects_self_loops_and_cycles(edges):
    with pytest.raises(dedup.DedupError) as info:
        dedup._assert_acyclic(edges, fx.ID, 2)
    assert info.value.arxiv_id == fx.ID and info.value.version == 2
    assert info.value.element_id in {"a", "b", "c"}


def test_acyclic_check_accepts_a_dag_with_two_parents():
    dedup._assert_acyclic([("s", "p1"), ("s", "p2"), ("p1", "h"), ("p2", "h")], fx.ID, 1)
