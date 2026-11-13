"""Tests for the regex rule-coverage analyzer in field_rules.py."""

import io
import itertools
import json
import random
import re

import pytest

from field_rules import (
    CoverageAnalyzer,
    Rule,
    Rulebook,
    UnsupportedPattern,
    cli,
    demo_rules,
    inspect_pattern,
    load_config,
)

ALPHABET = "AB"
BIG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def all_keys(alphabet, min_length, max_length):
    for length in range(min_length, max_length + 1):
        for product in itertools.product(alphabet, repeat=length):
            yield "".join(product)


# ---------------------------------------------------------------------------
# NFA vs the real re module
# ---------------------------------------------------------------------------

PATTERNS = [
    r"A",
    r"AB",
    r"[AB]",
    r"[^A]",
    r"A?B",
    r"AB{1,3}",
    r"(AB)+",
    r"(AB){2,3}",
    r"A|B",
    r"(AA|B){1,2}",
    r"[AB]{2,4}",
    r"A[B]{0,2}A",
    r"(A|B)*B",
    r"B{0,2}",
    r"(AB|BA){1,2}",
    r"[A][^B]{0,2}",
    r"^A",
    r"A$",
    r"^AB$",
    r"^A|B$",
    r"A*",
    r"(AB)+",
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_nfa_matches_re_search_on_full_universe(pattern):
    analyzer = CoverageAnalyzer(ALPHABET, 1, 5)
    inspected = analyzer.inspect(pattern)
    compiled = re.compile(pattern)
    for key in all_keys(ALPHABET, 1, 5):
        assert inspected.nfa.accepts_substring(key) == bool(compiled.search(key)), (
            pattern,
            key,
        )


def test_nfa_random_cross_check_on_three_symbol_alphabet():
    random.seed(20261114)
    alphabet = "ABC"
    analyzer = CoverageAnalyzer(alphabet, 1, 6)
    universe = list(all_keys(alphabet, 1, 6))
    templates = [
        r"AB*C",
        r"[AC]{1,4}",
        r"(AB|C){2,3}",
        r"[^B]{2}",
        r"A?B?C?",
        r"(AA|BB|CC){1,2}",
        r"[AB]{0,3}C",
    ]
    for pattern in templates:
        inspected = analyzer.inspect(pattern)
        compiled = re.compile(pattern)
        for key in random.sample(universe, 250):
            assert inspected.nfa.accepts_substring(key) == bool(compiled.search(key))


# ---------------------------------------------------------------------------
# Exact counting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", PATTERNS)
def test_matched_counts_are_exact(pattern):
    analyzer = CoverageAnalyzer(ALPHABET, 1, 5)
    inspected = analyzer.inspect(pattern)
    compiled = re.compile(pattern)
    expected = {}
    for length in range(1, 6):
        expected[length] = sum(
            1
            for product in itertools.product(ALPHABET, repeat=length)
            if compiled.search("".join(product))
        )
    assert analyzer._matched_counts(inspected.nfa) == expected


def test_count_includes_substring_matches_at_all_lengths():
    # "A" must match every non-empty string containing an A, at any length.
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    counts = analyzer._matched_counts(analyzer.inspect(r"A").nfa)
    assert counts == {1: 1, 2: 3, 3: 7, 4: 15}


# ---------------------------------------------------------------------------
# Rulebook dispatch
# ---------------------------------------------------------------------------


def test_rulebook_priority_and_classification():
    rules = [
        Rule("low", r"A", priority=30),
        Rule("high", r"AB", priority=10),
        Rule("middle", r"B", priority=20),
    ]
    book = Rulebook(rules)
    assert [rule.name for rule in book.rules] == ["high", "middle", "low"]
    assert book.winner("AB").name == "high"
    assert book.winner("BA").name == "middle"
    assert book.winner("CCC") is None
    buckets = book.classify(["AB", "BA"])
    assert buckets["high"] == ["AB"]
    assert buckets["middle"] == ["BA"]


def test_rulebook_implicit_priority_keeps_declaration_order():
    rules = [Rule("first", r"A"), Rule("second", r"B")]
    book = Rulebook(rules)
    assert [rule.name for rule in book.rules] == ["first", "second"]
    assert [rule.priority for rule in book.rules] == [0, 1]
    assert book.winner("AB").name == "first"


# ---------------------------------------------------------------------------
# Exhaustive coverage report
# ---------------------------------------------------------------------------


def test_fully_shadowed_rule_reports_covering_combination():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    rules = [
        Rule("high", r"[AB]{1,4}", priority=1, label="高优先级"),
        Rule("doomed", r"A", priority=2, label="被覆盖"),
    ]
    report = analyzer.analyze(rules)
    assert report.exhaustive is True
    coverage = report.rule_coverage["doomed"]
    assert coverage.fully_shadowed is True
    assert coverage.own_count == 0
    assert coverage.reachable_count > 0
    assert coverage.shadowed_count == coverage.reachable_count
    combos = {combo.combo: combo for combo in coverage.covering_combos}
    assert ("high",) in combos
    assert combos[("high",)].key_count == coverage.reachable_count
    assert coverage.sample_own_keys == []
    assert coverage.sample_shadowed_keys
    for key in coverage.sample_shadowed_keys:
        assert re.search(r"A", key)


def test_partial_shadowing_splits_counts_and_lists_combos():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    rules = [
        Rule("a_only", r"^A", priority=1),
        Rule("b_like", r"B", priority=2),
    ]
    report = analyzer.analyze(rules)
    coverage = report.rule_coverage["b_like"]
    assert coverage.fully_shadowed is False
    assert coverage.own_count > 0
    assert coverage.shadowed_count > 0
    assert coverage.own_count + coverage.shadowed_count == coverage.reachable_count
    # Every "own" sample really does lose to no higher priority rule.
    book = Rulebook(rules)
    for key in coverage.sample_own_keys:
        assert book.winner(key).name == "b_like"
    for key in coverage.sample_shadowed_keys:
        assert book.winner(key).name == "a_only"
    # The covering combination for every shadowed key is {a_only}.
    assert {combo.combo for combo in coverage.covering_combos} == {("a_only",)}


def test_covering_combination_uses_multiple_rules():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    rules = [
        Rule("starts_a", r"^A", priority=1),
        Rule("ends_b", r"B$", priority=2),
        Rule("target", r"[AB]{2}", priority=3),
    ]
    report = analyzer.analyze(rules)
    coverage = report.rule_coverage["target"]
    combo_names = {combo.combo for combo in coverage.covering_combos}
    # Some length-2 keys are blocked by the first rule, some by the second,
    # and "AB" is blocked by both at once.
    assert ("starts_a",) in combo_names
    assert ("ends_b",) in combo_names
    assert ("ends_b", "starts_a") in combo_names
    total = sum(combo.key_count for combo in coverage.covering_combos)
    assert total == coverage.shadowed_count


def test_unmatched_keys_are_real_and_cover_each_length():
    analyzer = CoverageAnalyzer(ALPHABET, 2, 4)
    rules = [Rule("only_ab", r"AB", priority=1)]
    report = analyzer.analyze(rules)
    assert report.unmatched_keys
    for key in report.unmatched_keys:
        assert not re.compile(r"AB").search(key)
        assert analyzer.min_length <= len(key) <= analyzer.max_length
    assert all(report.unmatched_existence_by_length.values())
    # Samples can be fed straight into the production matcher.
    book = Rulebook(rules)
    for key in report.unmatched_keys:
        assert book.winner(key) is None


def test_domain_fully_covered_has_no_unmatched_samples():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 3)
    report = analyzer.analyze([Rule("catch_all", r"[AB]{1,3}", priority=1)])
    assert report.unmatched_keys == []
    assert not any(report.unmatched_existence_by_length.values())


def test_counts_match_brute_force_on_small_domain():
    rules = [
        Rule("r1", r"ABA[AB]{0,2}", priority=1),
        Rule("r2", r"A{1,2}B{1,2}", priority=2),
        Rule("r3", r"B[AB]{2,3}", priority=3),
        Rule("r4", r"AAB{2,3}", priority=4),
        Rule("r5", r"^A", priority=5),
    ]
    analyzer = CoverageAnalyzer(ALPHABET, 2, 5, enumerate_cap=10**9)
    report = analyzer.analyze(rules)
    book = Rulebook(rules)
    brute = {rule.name: {"reachable": 0, "own": 0} for rule in book.rules}
    for key in all_keys(ALPHABET, 2, 5):
        matched = book.matching_rules(key)
        winner = book.winner(key)
        for rule in matched:
            brute[rule.name]["reachable"] += 1
        if winner is not None:
            brute[winner.name]["own"] += 1
    for rule in book.rules:
        coverage = report.rule_coverage[rule.name]
        assert coverage.reachable_count == brute[rule.name]["reachable"]
        assert coverage.own_count == brute[rule.name]["own"]
        assert coverage.shadowed_count == coverage.reachable_count - coverage.own_count
        assert coverage.estimated is False


# ---------------------------------------------------------------------------
# Scope and syntax boundaries
# ---------------------------------------------------------------------------


def test_out_of_alphabet_literal_is_reported():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    inspected = analyzer.inspect(r"AC")
    problems = analyzer._hard_scope_problems(inspected)
    assert any(
        "'C'" in problem and "outside the declared alphabet" in problem
        for problem in problems
    )
    report = analyzer.analyze([Rule("bad", r"AC", priority=1)])
    assert "bad" in report.scope_errors
    assert "bad" not in report.rule_coverage


def test_out_of_alphabet_character_class_member_is_reported():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    inspected = analyzer.inspect(r"[AC]")
    problems = analyzer._hard_scope_problems(inspected)
    assert any("U+0043" in problem for problem in problems)


def test_negated_class_stays_inside_alphabet():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 3)
    inspected = analyzer.inspect(r"[^A]")
    assert analyzer._hard_scope_problems(inspected) == []
    compiled = re.compile(r"[^A]")
    for key in all_keys(ALPHABET, 1, 3):
        assert inspected.nfa.accepts_substring(key) == bool(compiled.search(key))


def test_length_out_of_declared_range_is_soft_scope_error():
    analyzer = CoverageAnalyzer(ALPHABET, 2, 3)
    too_long = analyzer.inspect(r"[AB]{4,5}")
    problems = analyzer._soft_scope_problems(too_long)
    assert problems and "exceeds declared max length 3" in problems[0]
    # Search semantics: a short pattern can still win inside a longer key.
    assert analyzer._soft_scope_problems(analyzer.inspect(r"A")) == []


@pytest.mark.parametrize(
    "pattern",
    [r".B", r"A(?=B)", r"(?!A)B", r"(A)\1", r"\d", r"\w+", r"\bA\b"],
)
def test_unsupported_syntax_raises(pattern):
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    with pytest.raises(UnsupportedPattern):
        analyzer.inspect(pattern)


def test_unbounded_quantifier_without_cap_raises():
    with pytest.raises(UnsupportedPattern):
        inspect_pattern(r"A*", ALPHABET)
    with pytest.raises(UnsupportedPattern):
        inspect_pattern(r"(AB)+", ALPHABET)


def test_unbounded_quantifier_is_capped_to_declared_domain():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 5)
    inspected = analyzer.inspect(r"AB*A")
    compiled = re.compile(r"AB*A")
    for key in all_keys(ALPHABET, 1, 5):
        assert inspected.nfa.accepts_substring(key) == bool(compiled.search(key))
    # The intrinsic cap (5 B's) overshoots the domain, but every generated
    # string up to length 5 is identical to the unbounded pattern.
    assert inspected.max_length == 7
    assert inspected.min_length == 2
    # Every string of length >= 1 matches A* (empty match is a match).
    star = analyzer.inspect(r"A*")
    counts = analyzer._matched_counts(star.nfa)
    assert counts == {length: len(ALPHABET) ** length for length in range(1, 6)}


def test_unsupported_rule_is_recorded_not_fatal():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    report = analyzer.analyze(
        [
            Rule("ok", r"A", priority=1),
            Rule("bad", r"A.B", priority=2),
        ]
    )
    assert "bad" in report.scope_errors
    assert "ok" in report.rule_coverage


def test_invalid_regex_reports_clean_error():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    with pytest.raises(UnsupportedPattern):
        analyzer.inspect(r"(A")


# ---------------------------------------------------------------------------
# Stratified (large domain) mode
# ---------------------------------------------------------------------------


def test_stratified_counts_are_exact_and_samples_are_genuine():
    rules = [
        Rule("r1", r"AC[ABC]{1,3}", priority=1),
        Rule("r2", r"A{1,2}B{1,2}", priority=2),
        Rule("r3", r"B[ABC]{2,3}", priority=3),
        Rule("r4", r"AAB{2,3}", priority=4),
    ]
    alphabet = "ABC"
    stratified = CoverageAnalyzer(alphabet, 2, 6, enumerate_cap=1)
    assert stratified.exhaustive is False
    report = stratified.analyze(rules)
    exhaustive = CoverageAnalyzer(alphabet, 2, 6).analyze(rules)
    assert exhaustive.exhaustive is True
    for name, coverage in report.rule_coverage.items():
        assert coverage.estimated is True
        assert (
            coverage.reachable_count == exhaustive.rule_coverage[name].reachable_count
        )
        compiled = re.compile(coverage.pattern)
        for key in coverage.sample_own_keys + coverage.sample_shadowed_keys:
            assert compiled.search(key)
    assert report.universe_size == exhaustive.universe_size


def test_stratified_sampling_detects_full_shadowing_and_combos():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 3, enumerate_cap=1, sample_seed=7)
    report = analyzer.analyze(
        [
            Rule("broad", r"[AB]{1,3}", priority=1),
            Rule("narrow", r"A", priority=2),
        ]
    )
    coverage = report.rule_coverage["narrow"]
    assert coverage.estimated is True
    assert coverage.fully_shadowed is True
    assert coverage.shadowed_count == coverage.reachable_count
    assert coverage.own_count == 0
    assert coverage.sample_own_keys == []
    assert coverage.sample_shadowed_keys
    assert coverage.covering_combos[0].combo == ("broad",)


def test_stratified_different_seeds_change_samples_but_not_counts():
    rules = [Rule("r1", r"ABA[AB]{0,2}", priority=1), Rule("r2", r"A", priority=2)]
    base = CoverageAnalyzer(ALPHABET, 1, 4, enumerate_cap=1, sample_seed=0).analyze(
        rules
    )
    shifted = CoverageAnalyzer(ALPHABET, 1, 4, enumerate_cap=1, sample_seed=42).analyze(
        rules
    )
    for name in base.rule_coverage:
        assert (
            base.rule_coverage[name].reachable_count
            == shifted.rule_coverage[name].reachable_count
        )
    assert (
        base.rule_coverage["r1"].sample_own_keys
        != shifted.rule_coverage["r1"].sample_own_keys
    )


def test_stratified_unmatched_samples_are_real():
    analyzer = CoverageAnalyzer(BIG_ALPHABET, 4, 9, enumerate_cap=1)
    report = analyzer.analyze([Rule("rare", r"ZZZ", priority=1)])
    assert report.unmatched_keys
    compiled = re.compile(r"ZZZ")
    for key in report.unmatched_keys:
        assert not compiled.search(key)


# ---------------------------------------------------------------------------
# Sanity checks for the demo and text rendering
# ---------------------------------------------------------------------------


def test_demo_report_renders_and_is_consistent():
    analyzer = CoverageAnalyzer(BIG_ALPHABET, 4, 8)
    report = analyzer.analyze(demo_rules())
    assert report.exhaustive is False
    text = report.to_text()
    for rule in demo_rules():
        assert rule.name in text
    assert "UNMATCHED KEYS" in text
    for coverage in report.rule_coverage.values():
        assert coverage.own_count + coverage.shadowed_count == coverage.reachable_count


def test_inspect_pattern_length_bounds():
    inspected = inspect_pattern(r"AB{1,3}", ALPHABET)
    assert (inspected.min_length, inspected.max_length) == (2, 4)
    branched = inspect_pattern(r"(AAA|B){2}", ALPHABET)
    assert (branched.min_length, branched.max_length) == (2, 6)


def test_invalid_constructor_arguments():
    with pytest.raises(ValueError):
        CoverageAnalyzer("", 1, 2)
    with pytest.raises(ValueError):
        CoverageAnalyzer(ALPHABET, 0, 2)
    with pytest.raises(ValueError):
        CoverageAnalyzer(ALPHABET, 4, 2)
    with pytest.raises(ValueError):
        CoverageAnalyzer(["AB"], 1, 2)
    with pytest.raises(ValueError):
        inspect_pattern(r"A", ["A", "A"])


# ---------------------------------------------------------------------------
# Zero-width matches, JSON output and the CLI
# ---------------------------------------------------------------------------


def test_empty_match_pattern_covers_every_key():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 4)
    report = analyzer.analyze([Rule("star", r"A*", priority=1)])
    coverage = report.rule_coverage["star"]
    assert coverage.reachable_count == analyzer.universe_size
    assert coverage.own_count == analyzer.universe_size
    assert report.unmatched_keys == []


def test_report_to_dict_is_json_serializable():
    analyzer = CoverageAnalyzer(ALPHABET, 1, 3)
    report = analyzer.analyze(
        [
            Rule("r1", r"^A", priority=1),
            Rule("r2", r"B", priority=2),
        ]
    )
    encoded = json.dumps(report.to_dict(), ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["universe_size"] == analyzer.universe_size
    assert {entry["name"] for entry in decoded["rule_coverage"]} == {"r1", "r2"}
    entry = decoded["rule_coverage"][1]
    assert entry["own_count"] + entry["shadowed_count"] == entry["reachable_count"]
    assert isinstance(decoded["unmatched_existence_by_length"]["1"], bool)


def test_load_config_round_trip(tmp_path):
    config = {
        "alphabet": list(ALPHABET),
        "min_length": 1,
        "max_length": 3,
        "rules": [
            {"name": "first", "pattern": "^A", "priority": 1, "label": "料号"},
            {"name": "second", "pattern": "B"},
        ],
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    analyzer, rules = load_config(json.loads(path.read_text(encoding="utf-8")))
    assert [rule.name for rule in rules] == ["first", "second"]
    assert rules[1].priority is None
    report = analyzer.analyze(rules)
    assert "first" in report.rule_coverage


def test_cli_json_from_stdin(monkeypatch, capsys):
    config = {
        "alphabet": list(ALPHABET),
        "min_length": 1,
        "max_length": 2,
        "rules": [{"name": "r1", "pattern": "A", "priority": 1}],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(config)))
    assert cli(["--json", "-"]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["rule_coverage"][0]["name"] == "r1"
    assert decoded["exhaustive"] is True


def test_cli_text_output_and_invalid_config(tmp_path, capsys):
    config = {
        "alphabet": list(ALPHABET),
        "min_length": 1,
        "max_length": 2,
        "rules": [{"name": "r1", "pattern": "A", "priority": 1}],
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert cli([str(path)]) == 0
    assert "PER-RULE COVERAGE" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"alphabet": ["A"]}), encoding="utf-8")
    assert cli([str(bad)]) == 2
    assert "invalid configuration" in capsys.readouterr().err


def test_cli_without_arguments_runs_demo(capsys):
    assert cli([]) == 0
    assert "SCENARIO 1" in capsys.readouterr().out
