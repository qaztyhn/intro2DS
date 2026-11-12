
"""Offline tests for M2 semantic-content normalization."""

import pytest

from src.parsing.normalization import (
    normalize_block_math,
    normalize_citations,
    normalize_content,
    normalize_inline_math,
    normalize_whitespace,
    remove_formatting,
    strip_latex_comments,
)


def test_nested_formatting_keeps_visible_text():
    assert normalize_content(r"\textbf{Deep \emph{Learning}}") == "Deep Learning"


def test_layout_commands_removed_but_caption_and_label_preserved():
    result = normalize_content(r"\centering \toprule \caption{A table} \label{tab:a}")
    assert result == r"\caption{A table} \label{tab:a}"


def test_comments_removed_but_escaped_percent_survives():
    assert normalize_content("Accuracy is 95\\%. % comment\n Next line.") == r"Accuracy is 95\%. Next line."


def test_verbatim_percent_not_treated_as_comment():
    src = r"\begin{verbatim}x%y\end{verbatim}"
    assert strip_latex_comments(src) == src


def test_inline_verb_percent_not_treated_as_comment():
    src = r"\verb|x%y| % comment"
    assert strip_latex_comments(src) == r"\verb|x%y| "


def test_inline_math_parentheses_to_dollars():
    assert normalize_inline_math(r"Let \( x = 2 \).") == "Let $x = 2$."
    assert normalize_content(r"Let \( x = 2 \).") == "Let $x = 2$."


@pytest.mark.parametrize("source", [
    r"$$ E = mc^2 $$",
    r"\[ E = mc^2 \]",
    r"\begin{displaymath} E = mc^2 \end{displaymath}",
    r"\begin{equation*} E = mc^2 \end{equation*}",
])
def test_display_math_canonical(source):
    assert normalize_block_math(source) == r"\begin{equation*}E = mc^2\end{equation*}"


def test_align_preserved_instead_of_destroying_alignment():
    original = r"\begin{align} a &= b \\ c &= d \end{align}"
    assert normalize_block_math(original) == r"\begin{align}a &= b \\ c &= d\end{align}"


def test_citations_mapping_and_dedupe_preserve_order():
    src = r"Use \citep[see][p.~3]{oldA, oldB, unknown}."
    mapping = {"oldA": "canonical", "oldB": "canonical"}
    assert normalize_citations(src, mapping) == r"Use \citep[see][p.~3]{canonical,unknown}."


def test_cite_variations_and_unknown_keys():
    assert normalize_citations(r"\citet{x} and \cite{unknown}", {"x": "key"}) == (
        r"\citet{key} and \cite{unknown}"
    )


def test_does_not_modify_other_commands_containing_cite():
    assert normalize_citations(r"\citation{key}", {"key": "other"}) == r"\citation{key}"


def test_malformed_group_kept():
    assert remove_formatting(r"\textbf{missing") == r"\textbf{missing"


def test_math_commands_are_not_removed():
    assert normalize_content(r"$\alpha + \beta$") == r"$\alpha + \beta$"


def test_determinism_and_idempotence():
    source = r"\textbf{See \cite{old,alt}} and \( x = 2 \)."
    mapping = {"old": "new", "alt": "new"}
    once = normalize_content(source, mapping)
    assert once == r"See \cite{new} and $x = 2$."
    assert normalize_content(once, mapping) == once


def test_whitespace():
    assert normalize_whitespace("  a  \n b\t c  ") == "a b c"


def test_input_unchanged():
    source = r"\textit{Text}"
    normalize_content(source)
    assert source == r"\textit{Text}"


def test_rejects_non_string():
    with pytest.raises(TypeError):
        normalize_content(None)


def test_verbatim_unchanged_even_when_citations_are_normalized_outside():
    source = r"Text \verb|\cite{old}   100%| and \cite{old}."
    assert normalize_content(source, {"old": "new"}) == (
        r"Text \verb|\cite{old}   100%| and \cite{new}."
    )


def test_verbatim_environment_preserves_whitespace_and_layout():
    source = r"\begin{verbatim}\centering   a  % b\end{verbatim}"
    assert normalize_content(source) == source


@pytest.mark.parametrize("environment", [
    "align", "align*", "gather", "gather*", "multline", "multline*",
    "flalign", "flalign*", "alignat", "alignat*", "eqnarray", "eqnarray*",
])
def test_multirow_math_retains_environment_and_row_separators(environment):
    argument = "{2}" if environment.startswith("alignat") else ""
    source = (
        rf"\begin{{{environment}}}" + argument + "\n"
        + r"   a  &=  b   \label{eq:first}   \\ " + "\n"
        + r"   c  &=  d   \notag" + "\n"
        + rf"\end{{{environment}}}"
    )
    result = normalize_block_math(source)
    assert result.startswith(rf"\begin{{{environment}}}" + argument)
    assert result.endswith(rf"\end{{{environment}}}")
    assert r"\label{eq:first}" in result
    assert r"\notag" in result
    assert r"\\" in result
    assert result.count(r"\begin{") == 1
    assert normalize_block_math(result) == result


def test_numbered_equation_remains_numbered_with_label_and_tag():
    source = r"\begin{equation} x = 2 \label{eq:x}\tag{X} \end{equation}"
    assert normalize_block_math(source) == (
        r"\begin{equation}x = 2 \label{eq:x}\tag{X}\end{equation}"
    )


def test_equation_star_remains_unnumbered():
    source = r"\begin{equation*} x^2 \end{equation*}"
    assert normalize_block_math(source) == r"\begin{equation*}x^2\end{equation*}"


def test_unified_unnumbered_forms_are_exactly_equal():
    sources = [
        r"$$ x+y $$", r"\[ x+y \]",
        r"\begin{displaymath} x+y \end{displaymath}",
        r"\begin{equation*} x+y \end{equation*}",
    ]
    assert len({normalize_content(source) for source in sources}) == 1


def test_math_text_whitespace_not_destroyed_by_parent_cleanup():
    src = r"In text  \begin{align*} x &= \text{a   b} \\ y &= z \end{align*}  after."
    result = normalize_content(src)
    assert r"\text{a   b}" in result
    assert r"x &= \text{a   b} \\ y &= z" in result
    assert result.startswith("In text ")
    assert result.endswith(" after.")


def test_nested_aligned_inside_equation_remains_nested():
    src = (
        r"\begin{equation} "
        r"\begin{aligned} a &= b \\ c &= d \end{aligned} "
        r"\label{eq:both} \end{equation}"
    )
    result = normalize_content(src)
    assert result.count(r"\begin{equation}") == 1
    assert result.count(r"\begin{aligned}") == 1
    assert r"a &= b \\ c &= d" in result
    assert r"\label{eq:both}" in result
    assert normalize_content(result) == result


def test_malformed_environment_kept_instead_of_dropped():
    src = r"\begin{align} a &= b \end{gather}"
    assert normalize_block_math(src) == src


def test_math_normalizer_ignores_verbatim_display():
    source = r"\verb|$$ x $$|  $$ y $$"
    assert normalize_block_math(source) == (
        r"\verb|$$ x $$|  \begin{equation*}y\end{equation*}"
    )


def test_full_normalization_with_cites_in_multirow_math():
    src = (
        r"\begin{align} "
        r"x &= \text{see \cite{old}} \\ y &= 1 "
        r"\end{align}"
    )
    result = normalize_content(src, {"old": "canonical"})
    assert r"\cite{canonical}" in result
    assert r"\begin{align}" in result
    assert r"\\" in result
    assert normalize_content(result, {"old": "canonical"}) == result


def test_two_math_blocks_in_single_content_remain_distinct():
    src = r"Before $$ a $$ between \begin{gather*} b \\ c \end{gather*} after"
    result = normalize_content(src)
    assert result.count(r"\begin{equation*}") == 1
    assert result.count(r"\begin{gather*}") == 1
    assert " between " in result


def test_escaped_dollar_is_not_treated_as_inline_math():
    src = r"Price \$50 and math $a  +  b$ finish."
    result = normalize_content(src)
    assert r"\$50" in result
    assert "$a + b$" in result


def test_inline_dollar_and_parentheses_have_same_canonical_content():
    sources = [r"\( a  +   b \)", "$a + b$", "$  a + b  $"]
    assert len({normalize_inline_math(text) for text in sources}) == 1
    assert normalize_inline_math(sources[0]) == "$a + b$"


def test_inline_math_text_spacing_preserved():
    source = r"A $\text{a   b}  +  x$ B"
    assert normalize_content(source) == r"A $\text{a   b} + x$ B"


def test_inline_normalization_skips_display_math():
    source = r"$$ a   + b $$ and \(x\)"
    assert normalize_inline_math(source) == r"$$ a   + b $$ and $x$"


def test_inline_unbalanced_kept_intact():
    assert normalize_inline_math(r"Unmatched \( x = 2") == r"Unmatched \( x = 2"


def test_inline_math_environment_is_canonical_dollar_math():
    source = r"Let \begin{math} a  +  b \end{math} be the result."
    assert normalize_content(source) == "Let $a + b$ be the result."


def test_math_texttt_command_not_removed_from_inline_formula():
    source = r"We use $\texttt{a  b} + x$ and \textbf{text}."
    result = normalize_content(source)
    assert r"$\texttt{a  b} + x$" in result
    assert result.endswith("and text.")


def test_math_texttt_command_not_removed_from_parenthesis_formula():
    source = r"We use \(\texttt{a   b}  + x\)."
    result = normalize_content(source)
    assert r"$\texttt{a   b} + x$" in result
