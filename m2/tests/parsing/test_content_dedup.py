"""Regressions for exact cross-version matching, independent of hierarchy context."""
import copy

import pytest

from m2.src.parsing.deduplication import deduplicate_with_stats
from m2.src.parsing.export import export_tables
from m2.src.parsing.validation import validate_tables
from .fixtures import tv4_builders as fx


@pytest.mark.parametrize('kind', [
    'part', 'chapter', 'section', 'subsection', 'subsubsection',
    'paragraph_heading', 'subparagraph_heading', 'abstract', 'appendix',
    'acknowledgements', 'list', 'list_item', 'custom_structure',
])
def test_every_structural_kind_can_move_to_a_different_parent(kind):
    def component():
        return fx.node(kind, 'Shared component', fx.sentence('Shared sentence.'))

    docs = [fx.doc(1, fx.root(fx.section('Old parent', component()))),
            fx.doc(2, fx.root(fx.section('New parent', fx.sentence('Inserted.'), component())))]
    before = copy.deepcopy(docs)
    elements, hierarchy, _ = deduplicate_with_stats(docs)
    matches = [e for e in elements if e['kind'] == kind and e['content'] == 'Shared component']
    assert len(matches) == 1
    edges = [r for r in hierarchy if r['element_id'] == matches[0]['element_id']]
    assert {r['version'] for r in edges} == {1, 2}
    assert len({r['parent_id'] for r in edges}) == 2
    assert docs == before
    assert deduplicate_with_stats(docs) == deduplicate_with_stats(list(reversed(docs)))


def test_structural_repeats_in_one_version_keep_children_separate_across_versions():
    def tree(parent):
        return fx.root(fx.section(parent,
            fx.subsection('Results', fx.sentence('First branch.')),
            fx.subsection('Results', fx.sentence('Second branch.'))))

    elements, hierarchy, stats = deduplicate_with_stats([fx.doc(1, tree('Old')), fx.doc(2, tree('New'))])
    results = {e['element_id'] for e in elements if e['content'] == 'Results'}
    assert len(results) == 2  # not four from parent-based keys, not one mixing children
    for version in (1, 2):
        for identity in results:
            assert len([r for r in hierarchy if r['version'] == version and r['element_id'] == identity]) == 1
            assert len([r for r in hierarchy if r['version'] == version and r['parent_id'] == identity]) == 1
    assert any('distinct structural occurrence' in w for w in stats['warnings'])


def test_changed_structural_content_is_not_matched():
    docs = [fx.doc(1, fx.root(fx.section('Method', fx.sentence('Same.')))),
            fx.doc(2, fx.root(fx.section('Methods', fx.sentence('Same.'))))]
    elements, _, _ = deduplicate_with_stats(docs)
    assert len([e for e in elements if e['kind'] == 'section']) == 2
    assert len([e for e in elements if e['kind'] == 'sentence']) == 1


def test_parent_child_roles_can_reverse_between_versions_without_a_cycle(tmp_path):
    docs = [fx.doc(1, fx.root(fx.node('list', 'A', fx.node('list', 'B', fx.sentence('Text.'))))),
            fx.doc(2, fx.root(fx.node('list', 'B', fx.node('list', 'A', fx.sentence('Text.')))))]
    elements, hierarchy, _ = deduplicate_with_stats(docs)
    assert len([e for e in elements if e['kind'] == 'list']) == 2
    export_tables(elements, hierarchy, [], tmp_path / 'out')
    assert validate_tables(tmp_path / 'out')['passed']
    # The union across versions can have opposite edges; each version must be a DAG.
    edges = [{(r['element_id'], r['parent_id']) for r in hierarchy if r['version'] == v}
             for v in (1, 2)]
    assert any((parent, child) in edges[1] for child, parent in edges[0])


def test_non_ancestor_reuse_cycle_is_prevented_before_export(tmp_path):
    docs = [fx.doc(1, fx.root(
        fx.node('paragraph', 'A', fx.node('paragraph', 'B')),
        fx.node('paragraph', 'B', fx.node('paragraph', 'A'))))]
    elements, hierarchy, stats = deduplicate_with_stats(docs)
    assert len([e for e in elements if e['kind'] == 'paragraph']) == 3
    assert len(hierarchy) == 4
    assert any('cycle' in warning for warning in stats['warnings'])
    export_tables(elements, hierarchy, [], tmp_path / 'out')
    assert validate_tables(tmp_path / 'out')['passed']


def test_nested_equal_content_representatives_are_reused_in_later_versions():
    def tree():
        return fx.root(fx.node('paragraph', 'X', fx.node('paragraph', 'X', fx.sentence('X'))))

    elements, hierarchy, _ = deduplicate_with_stats([fx.doc(1, tree()), fx.doc(2, tree())])
    assert len([e for e in elements if e['kind'] == 'paragraph']) == 2
    for identity in [e['element_id'] for e in elements if e['kind'] == 'paragraph']:
        assert {r['version'] for r in hierarchy if r['element_id'] == identity} == {1, 2}
