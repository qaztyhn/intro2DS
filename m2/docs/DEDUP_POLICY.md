# Exact cross-version element matching

Priority: [official M2 requirement](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/parsing/)
first, then the team contract (sections 2.4, 3.4, 7.2 and 10). This policy
supersedes D2's parent-dependent matching in `decisions/dedup_export.md`.

## Identity versus placement

Every non-root kind is matched by exact `(kind, normalized_content)` within a
publication. Normalization and citation-key rewriting happen in the hierarchy
parser before deduplication. Dedup itself does not normalize again or use fuzzy
matching. Keeping kind in the key prevents a paragraph and its sentence with
equal strings from collapsing into one node. Nothing matches across publications.

Parent, source filename, local ID, sibling position and version number are not
part of the cross-version content key. An unchanged subsection or list can move
under a renamed parent and retain its element ID. Its edges are recorded
separately per version. The contents of a heading node are the normalized heading
text supplied by TV2; changing its descendants does not change that stored text.
The descendant content and edges are matched/recorded in their own right.

## Repeated occurrences and the four-column schema

Matching identical text does not imply that different structural occurrences in
one version are the same occurrence. For example two `Results` headings can have
different children. Collapsing both to one ID in that version would attach both
sets of children to both headings, since the official hierarchy has no occurrence
ID. Nested empty list wrappers could even form a self-loop.

For structural kinds, use a pool of representatives under the same content key.
Each representative can be used at most once in one version. Reuse available
representatives from earlier versions in deterministic first-encounter order,
regardless of their old parent or position. Allocate another only when the pool
cannot represent a distinct occurrence safely. The pool size therefore reflects
the required multiplicity, not a parent change. Such splits are recorded as
warnings. Repeated equal content with indistinguishable labels has no guaranteed
semantic occurrence correspondence across versions; edges still preserve each
version's own structure and order.

Sentence, equation, figure and paragraph contents can also share within a version,
subject to cycle safety, retaining the existing repeated-edge/position convention.
Structural multiplicity is a representational exception, not a claim that duplicate
rows meet unrestricted content uniqueness. It is necessary to preserve the source
hierarchy under the fixed schema; the tests make this boundary explicit.

## Safety and export

Before reusing an ID, reject it if the proposed edge would create a cycle in the
current version's graph (including paths outside the current ancestor stack).
Try another representative; allocate and warn if none is safe. Validate the final
graph again. Do not silently drop an edge or let one unsafe merge fail the whole
publication. Actual cyclic input node objects are still rejected as malformed.
Opposite edges in different versions are allowed; versions are not one graph.

Root IDs remain version-specific as in the official example. No-TeX versions
produce no parsed elements, but remain in QA. The shared dataclasses, function
signatures and final Parquet schemas have not changed. Tests include changed
parents/positions, exact versus changed content, separate kinds/publications,
structural multiplicity, nested and non-ancestor cycles, stable IDs, immutable
inputs and export validation. These tests do not establish real-corpus coverage.
