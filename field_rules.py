"""Regex rule-coverage analyzer for supplier dynamic-field extraction.

Procurement contracts are parsed by an ordered list of regex rules that sort
supplier keys into fields such as part number (料号), model (型号) or batch
number (批号).  Before promoting a new (lower priority) rule it must be known
which keys, inside the *declared* alphanumeric alphabet and length range, the
rule can still win and which are shadowed by higher priority rules.

The analyzer is intentionally restricted to a finite, well defined subset of
regex syntax (bounded quantifiers, literals, character classes - including
negated classes - alternation and groups).  Everything outside that subset or
outside the declared alphabet is reported instead of being guessed.

Public API
----------
Rulebook
    Ordered collection of compiled regex rules with priority based dispatch.
CoverageAnalyzer
    Builds the coverage report for one declared alphabet / length domain.
CoverageReport
    Structured result: scope violations, per-rule reachable/shadowed counts,
    covering rule combinations, effective sample keys and unmatched keys.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import sys
import warnings
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

# sre_parse/sre_constants are deprecated on 3.11+ but remain the most direct
# parser API; silence the deprecation noise while importing them.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import sre_constants
    import sre_parse

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnsupportedPattern(ValueError):
    """Raised when a pattern uses regex features the analyzer cannot model."""


# ---------------------------------------------------------------------------
# NFA construction (Thompson construction on top of sre_parse)
# ---------------------------------------------------------------------------

_EPSILON = None
_START_ANCHOR: Literal["AT_START"] = "AT_START"
_END_ANCHOR: Literal["AT_END"] = "AT_END"
_ANCHORS = (_START_ANCHOR, _END_ANCHOR)
Label = Optional[Union[FrozenSet[int], Literal["AT_START", "AT_END"]]]


class _NFA:
    """Acyclic epsilon NFA with character-set edges and anchor edges.

    Edge labels are ``None`` (epsilon), a ``frozenset`` of code points, or one
    of the anchor markers.  Quantifiers are required to be bounded, so the
    graph stays acyclic.  ``re.search`` semantics are obtained by restarting
    the automaton at every input position.
    """

    def __init__(self) -> None:
        self.edges: Dict[int, List[Tuple[int, Label]]] = {}
        self.start = self._new_state()
        self.accept = self._new_state()

    def _new_state(self) -> int:
        state = len(self.edges)
        self.edges[state] = []
        return state

    def add_edge(self, src: int, dst: int, label: Label = _EPSILON) -> None:
        self.edges[src].append((dst, label))

    def closure(
        self,
        states: Iterable[int],
        at_start: bool = False,
        at_end: bool = False,
    ) -> FrozenSet[int]:
        pending = list(states)
        seen = set(pending)
        while pending:
            state = pending.pop()
            for target, label in self.edges[state]:
                if label is _EPSILON:
                    allowed = True
                elif label == _START_ANCHOR:
                    allowed = at_start
                elif label == _END_ANCHOR:
                    allowed = at_end
                else:
                    allowed = False
                if allowed and target not in seen:
                    seen.add(target)
                    pending.append(target)
        return frozenset(seen)

    def move(self, states: FrozenSet[int], char: int) -> FrozenSet[int]:
        """Consume one character and restart at the new position (search)."""
        destinations: List[int] = []
        for state in states:
            for target, label in self.edges[state]:
                if label not in _ANCHORS and label is not _EPSILON and char in label:
                    destinations.append(target)
        advanced = self.closure(destinations)
        restart = self.closure([self.start])
        return advanced | restart

    def accepts_substring(self, text: str) -> bool:
        """re.search semantics, including ``^`` / ``$`` anchor handling."""
        active = self.closure([self.start], at_start=True)
        if self.accept in active:
            return True
        last_index = len(text) - 1
        for index, char in enumerate(text):
            active = self.move(active, ord(char))
            if index == last_index:
                active = self.closure(active, at_end=True)
            if self.accept in active:
                return True
        return False

    def distance_bounds(self) -> Tuple[Dict[int, int], Dict[int, int]]:
        """Shortest/longest char-edge distance from each state to accept.

        The graph is a DAG (edges always point at higher state numbers), so a
        single descending sweep solves both.  Unreachable-to-accept states get
        ``shortest = +inf`` and ``longest = -inf``.
        """
        infinite = 10**12
        shortest: Dict[int, int] = {}
        longest: Dict[int, int] = {}
        for state in range(len(self.edges) - 1, -1, -1):
            if state == self.accept:
                shortest[state] = 0
                longest[state] = 0
                continue
            best_short = infinite
            best_long = -infinite
            for target, label in self.edges[state]:
                weight = 0 if label is _EPSILON or label in _ANCHORS else 1
                candidate_short = weight + shortest[target]
                candidate_long = weight + longest[target]
                if candidate_short < best_short:
                    best_short = candidate_short
                if candidate_long > best_long:
                    best_long = candidate_long
            shortest[state] = best_short
            longest[state] = best_long
        return shortest, longest


class _NFABuilder:
    def __init__(self, universe: FrozenSet[int], length_cap: Optional[int]) -> None:
        self.universe = universe
        self.length_cap = length_cap
        self.nfa = _NFA()
        self.out_of_scope: Dict[int, str] = {}
        self.unsupported: List[str] = []

    def build(self, sub: Any) -> Tuple[int, int]:
        start = self.nfa._new_state()
        accept = start
        for op, av in sub:
            fragment_start, fragment_accept = self._build_op(op, av)
            self.nfa.add_edge(accept, fragment_start)
            accept = fragment_accept
        end = self.nfa._new_state()
        self.nfa.add_edge(accept, end)
        return start, end

    def _build_op(self, op, av) -> Tuple[int, int]:
        if op is sre_constants.LITERAL:
            if av not in self.universe:
                self.out_of_scope[av] = "used as a literal"
            return self._char_edge(frozenset([av]))
        if op is sre_constants.NOT_LITERAL:
            return self._char_edge(self.universe - frozenset([av]))
        if op is sre_constants.IN:
            return self._build_in(av)
        if op is sre_constants.BRANCH:
            return self._build_branch(av[1])
        if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
            return self._build_repeat(av)
        if op is sre_constants.SUBPATTERN:
            return self.build(av[3])
        if op is sre_constants.AT:
            if av in (sre_constants.AT_BEGINNING, sre_constants.AT_BEGINNING_STRING):
                return self._anchor_edge(_START_ANCHOR)
            if av in (sre_constants.AT_END, sre_constants.AT_END_STRING):
                return self._anchor_edge(_END_ANCHOR)
            self.unsupported.append("word-boundary anchor %s" % _op_name(av))
            return self._epsilon_edge()
        self.unsupported.append("unsupported opcode %s" % _op_name(op))
        return self._epsilon_edge()

    def _anchor_edge(self, marker: Literal["AT_START", "AT_END"]) -> Tuple[int, int]:
        start = self.nfa._new_state()
        accept = self.nfa._new_state()
        self.nfa.add_edge(start, accept, marker)
        return start, accept

    def _char_edge(self, chars: FrozenSet[int]) -> Tuple[int, int]:
        start = self.nfa._new_state()
        accept = self.nfa._new_state()
        self.nfa.add_edge(start, accept, chars)
        return start, accept

    def _epsilon_edge(self) -> Tuple[int, int]:
        start = self.nfa._new_state()
        accept = self.nfa._new_state()
        self.nfa.add_edge(start, accept)
        return start, accept

    def _build_in(self, items) -> Tuple[int, int]:
        negated = False
        included: set = set()
        for op, av in items:
            if op is sre_constants.NEGATE:
                negated = True
            elif op is sre_constants.LITERAL:
                included.add(av)
            elif op is sre_constants.RANGE:
                included.update(range(av[0], av[1] + 1))
            elif op is sre_constants.NOT_LITERAL:
                included.update(self.universe - frozenset([av]))
            else:
                self.unsupported.append("character class member %s" % _op_name(op))
        for code in included:
            if code not in self.universe:
                self.out_of_scope[code] = "used in character class"
        chars = frozenset(included) & self.universe
        if negated:
            chars = self.universe - chars
        return self._char_edge(chars)

    def _build_branch(self, branches) -> Tuple[int, int]:
        start = self.nfa._new_state()
        accept = self.nfa._new_state()
        for branch in branches:
            fragment_start, fragment_accept = self.build(branch)
            self.nfa.add_edge(start, fragment_start)
            self.nfa.add_edge(fragment_accept, accept)
        return start, accept

    def _build_repeat(self, av) -> Tuple[int, int]:
        minimum, maximum, sub = av
        if maximum == sre_constants.MAXREPEAT:
            maximum = _fitted_repeat_cap(sub, self.length_cap)
            if maximum is None:
                self.unsupported.append(
                    "unbounded quantifier (pass a declared max length or use an "
                    "explicit upper bound)"
                )
                maximum = minimum if minimum else 1
        start = self.nfa._new_state()
        accept = start
        for _ in range(minimum):
            fragment_start, fragment_accept = self.build(sub)
            self.nfa.add_edge(accept, fragment_start)
            accept = fragment_accept
        for _ in range(maximum - minimum):
            fragment_start, fragment_accept = self.build(sub)
            self.nfa.add_edge(accept, fragment_start)
            self.nfa.add_edge(accept, fragment_accept)
            accept = fragment_accept
        end = self.nfa._new_state()
        self.nfa.add_edge(accept, end)
        return start, end


def _op_name(op) -> str:
    try:
        return str(op).split(".", 1)[-1].lower()
    except Exception:  # pragma: no cover - defensive
        return repr(op)


# ---------------------------------------------------------------------------
# Pattern inspection
# ---------------------------------------------------------------------------


@dataclass
class InspectedPattern:
    raw: str
    parsed: Any
    nfa: _NFA
    min_length: int
    max_length: int
    out_of_scope: Dict[int, str]
    unsupported: List[str]

    @property
    def supported(self) -> bool:
        return not self.unsupported and not self.out_of_scope


def _fitted_repeat_cap(sub: Any, length_cap: Optional[int]) -> Optional[int]:
    """Largest repetition count of *sub* that fits inside *length_cap*."""
    if length_cap is None:
        return None
    sub_min, _ = _walk_lengths(sub, length_cap)
    if sub_min == 0:
        return length_cap
    return length_cap // sub_min


def _walk_lengths(sub: Any, length_cap: Optional[int] = None) -> Tuple[int, int]:
    minimum = 0
    maximum = 0
    for op, av in sub:
        if op in (sre_constants.LITERAL, sre_constants.NOT_LITERAL, sre_constants.IN):
            minimum += 1
            maximum += 1
        elif op is sre_constants.AT:
            if av not in (
                sre_constants.AT_BEGINNING,
                sre_constants.AT_BEGINNING_STRING,
                sre_constants.AT_END,
                sre_constants.AT_END_STRING,
            ):
                raise UnsupportedPattern(
                    "anchor %s is outside the supported subset" % _op_name(av)
                )
            continue
        elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
            rep_min, rep_max, rep_sub = av
            sub_min, sub_max = _walk_lengths(rep_sub, length_cap)
            minimum += rep_min * sub_min
            if rep_max == sre_constants.MAXREPEAT:
                rep_max = _fitted_repeat_cap(rep_sub, length_cap)
                if rep_max is None:
                    raise UnsupportedPattern(
                        "unbounded quantifier; pass a declared max length or use "
                        "an explicit upper bound such as {0,10}"
                    )
            maximum += rep_max * sub_max
        elif op is sre_constants.BRANCH:
            branch_ranges = [_walk_lengths(branch, length_cap) for branch in av[1]]
            minimum += min(low for low, _ in branch_ranges)
            maximum += max(high for _, high in branch_ranges)
        elif op is sre_constants.SUBPATTERN:
            low, high = _walk_lengths(av[3], length_cap)
            minimum += low
            maximum += high
        else:
            raise UnsupportedPattern(
                "opcode %s is outside the supported subset" % _op_name(op)
            )
    return minimum, maximum


def inspect_pattern(
    pattern: str,
    alphabet: Sequence[str],
    length_cap: Optional[int] = None,
) -> InspectedPattern:
    """Parse *pattern* and build its NFA against the declared alphabet.

    Raises :class:`UnsupportedPattern` for syntax outside the supported subset.
    Characters outside the declared alphabet do not raise; they are recorded on
    the returned object so callers can report them as scope violations.
    Unbounded quantifiers (``*``/``+``) are accepted when *length_cap* is
    given; they are expanded up to that many repetitions.
    """
    universe = frozenset(ord(char) for char in alphabet)
    if len(universe) != len(alphabet):
        raise ValueError("alphabet contains duplicate characters")
    try:
        parsed = sre_parse.parse(pattern, flags=0)
    except re.error as exc:
        raise UnsupportedPattern("pattern %r is not a valid regex: %s" % (pattern, exc))
    min_length, max_length = _walk_lengths(parsed, length_cap)
    builder = _NFABuilder(universe, length_cap)
    start, accept = builder.build(parsed)
    builder.nfa.start = start
    builder.nfa.accept = accept
    if builder.unsupported:
        raise UnsupportedPattern(
            "pattern %r uses unsupported features: %s"
            % (pattern, "; ".join(builder.unsupported))
        )
    return InspectedPattern(
        raw=pattern,
        parsed=parsed,
        nfa=builder.nfa,
        min_length=min_length,
        max_length=max_length,
        out_of_scope=dict(builder.out_of_scope),
        unsupported=list(builder.unsupported),
    )


# ---------------------------------------------------------------------------
# Rule book: the actual matcher used in production
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    name: str
    pattern: str
    # Populated by Rulebook / CoverageAnalyzer; cast keeps the populated type
    # visible while preserving None defaults for convenient construction.
    priority: int = cast(int, None)
    label: str = ""
    compiled: re.Pattern[str] = cast("re.Pattern[str]", None)
    inspected: InspectedPattern = cast(InspectedPattern, None)


class Rulebook:
    """Ordered regex rules.  Lower priority numbers win; ties keep order."""

    def __init__(self, rules: Sequence[Rule], flags: int = 0) -> None:
        ordered: List[Tuple[int, int, Rule]] = []
        for index, rule in enumerate(rules):
            rule.compiled = re.compile(rule.pattern, flags)
            rule.priority = index if rule.priority is None else rule.priority
            ordered.append((rule.priority, index, rule))
        ordered.sort(key=lambda item: (item[0], item[1]))
        self.rules = [rule for _, _, rule in ordered]
        self._by_name = {rule.name: rule for rule in self.rules}

    def matching_rules(self, key: str) -> List[Rule]:
        return [rule for rule in self.rules if rule.compiled.search(key)]

    def winner(self, key: str) -> Optional[Rule]:
        for rule in self.rules:
            if rule.compiled.search(key):
                return rule
        return None

    def classify(self, keys: Iterable[str]) -> Dict[str, List[str]]:
        buckets: Dict[str, List[str]] = {rule.name: [] for rule in self.rules}
        buckets["__unmatched__"] = []
        for key in keys:
            winner = self.winner(key)
            buckets[winner.name if winner else "__unmatched__"].append(key)
        return buckets


# ---------------------------------------------------------------------------
# Coverage report data classes
# ---------------------------------------------------------------------------


@dataclass
class ComboCoverage:
    combo: Tuple[str, ...]
    key_count: int
    share_of_reachable: float


@dataclass
class RuleCoverage:
    name: str
    label: str
    pattern: str
    priority: int
    min_length: int
    max_length: int
    reachable_count: int
    shadowed_count: int
    own_count: int
    fully_shadowed: bool
    covering_combos: List[ComboCoverage]
    sample_own_keys: List[str]
    sample_shadowed_keys: List[str]
    estimated: bool


@dataclass
class CoverageReport:
    alphabet: Tuple[str, ...]
    min_length: int
    max_length: int
    universe_size: int
    exhaustive: bool
    scope_errors: Dict[str, List[str]]
    rule_coverage: Dict[str, RuleCoverage]
    unmatched_keys: List[str]
    unmatched_existence_by_length: Dict[int, bool]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable representation of the whole report."""
        return {
            "alphabet": list(self.alphabet),
            "min_length": self.min_length,
            "max_length": self.max_length,
            "universe_size": self.universe_size,
            "exhaustive": self.exhaustive,
            "scope_errors": self.scope_errors,
            "rule_coverage": [
                {
                    "name": coverage.name,
                    "label": coverage.label,
                    "pattern": coverage.pattern,
                    "priority": coverage.priority,
                    "match_length": [coverage.min_length, coverage.max_length],
                    "estimated": coverage.estimated,
                    "reachable_count": coverage.reachable_count,
                    "shadowed_count": coverage.shadowed_count,
                    "own_count": coverage.own_count,
                    "fully_shadowed": coverage.fully_shadowed,
                    "covering_combos": [
                        {
                            "combo": list(combo.combo),
                            "key_count": combo.key_count,
                            "share_of_reachable": combo.share_of_reachable,
                        }
                        for combo in coverage.covering_combos
                    ],
                    "sample_own_keys": coverage.sample_own_keys,
                    "sample_shadowed_keys": coverage.sample_shadowed_keys,
                }
                for coverage in sorted(
                    self.rule_coverage.values(), key=lambda item: item.priority
                )
            ],
            "unmatched_keys": self.unmatched_keys,
            "unmatched_existence_by_length": {
                str(length): exists
                for length, exists in sorted(self.unmatched_existence_by_length.items())
            },
        }

    def to_text(self) -> str:
        lines: List[str] = []
        lines.append(
            "declared alphabet : %r (%d symbols)"
            % ("".join(self.alphabet), len(self.alphabet))
        )
        lines.append(
            "declared length   : %d..%d (universe size %d, %s enumeration)"
            % (
                self.min_length,
                self.max_length,
                self.universe_size,
                "exhaustive" if self.exhaustive else "stratified",
            )
        )
        if self.scope_errors:
            lines.append("")
            lines.append("SCOPE / SYNTAX PROBLEMS")
            for name, problems in self.scope_errors.items():
                lines.append("  - %s" % name)
                for problem in problems:
                    lines.append("      * %s" % problem)
        lines.append("")
        lines.append("PER-RULE COVERAGE (priority order)")
        ordered = sorted(self.rule_coverage.values(), key=lambda item: item.priority)
        for coverage in ordered:
            tag = "ESTIMATED" if coverage.estimated else "EXACT"
            lines.append(
                "  [%d] %s (%s)  pattern=%r  match-length %d..%d  [%s]"
                % (
                    coverage.priority,
                    coverage.name,
                    coverage.label or "-",
                    coverage.pattern,
                    coverage.min_length,
                    coverage.max_length,
                    tag,
                )
            )
            lines.append(
                "      reachable=%d  shadowed=%d  own=%d  fully_shadowed=%s"
                % (
                    coverage.reachable_count,
                    coverage.shadowed_count,
                    coverage.own_count,
                    coverage.fully_shadowed,
                )
            )
            if coverage.covering_combos:
                lines.append("      covering combinations:")
                for combo in coverage.covering_combos:
                    lines.append(
                        "          %-28s keys=%d (%.2f%% of reachable)"
                        % (
                            "{" + ", ".join(combo.combo) + "}",
                            combo.key_count,
                            combo.share_of_reachable * 100.0,
                        )
                    )
            if coverage.sample_own_keys:
                lines.append(
                    "      sample keys it wins : %s"
                    % _preview(coverage.sample_own_keys)
                )
            if coverage.sample_shadowed_keys:
                lines.append(
                    "      sample shadowed keys: %s"
                    % _preview(coverage.sample_shadowed_keys)
                )
        lines.append("")
        if self.unmatched_keys:
            lines.append("UNMATCHED KEYS (no rule fires; matcher-ready samples)")
            for key in self.unmatched_keys:
                lines.append("  - %r" % key)
        else:
            lines.append(
                "UNMATCHED KEYS: none (the rule book covers the whole declared domain)"
            )
        return "\n".join(lines)


def _preview(values: Sequence[str], limit: int = 8) -> str:
    shown = values[:limit]
    rendered = ", ".join(repr(value) for value in shown)
    if len(values) > limit:
        rendered += ", ... (+%d more)" % (len(values) - limit)
    return rendered


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class CoverageAnalyzer:
    """Analyze rule coverage over a declared finite key domain.

    Parameters
    ----------
    alphabet:
        Every character that supplier keys are allowed to use, e.g.
        ``"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"``.
    min_length, max_length:
        Declared inclusive key-length range.
    enumerate_cap:
        If the universe (sum of ``len(alphabet) ** n`` over the length range)
        is at most this many keys every key is enumerated exactly.  Above the
        cap the analyzer switches to stratified analysis: counts remain exact
        (subset-construction counting) while covering combinations and sample
        keys are derived from bounded deterministic walks and marked as
        estimates.
    sample_budget:
        Maximum number of concrete keys kept per category / length in
        stratified mode.
    """

    def __init__(
        self,
        alphabet: Iterable[str],
        min_length: int,
        max_length: int,
        enumerate_cap: int = 300_000,
        sample_budget: int = 25,
        sample_seed: int = 0,
    ) -> None:
        chars = tuple(alphabet)
        if min_length < 1 or max_length < min_length:
            raise ValueError("invalid length range %d..%d" % (min_length, max_length))
        if not chars:
            raise ValueError("alphabet must not be empty")
        if any(len(char) != 1 for char in chars):
            raise ValueError("alphabet entries must be single characters")
        self.alphabet = chars
        self.universe = frozenset(ord(char) for char in chars)
        self.min_length = min_length
        self.max_length = max_length
        self.enumerate_cap = enumerate_cap
        self.sample_budget = sample_budget
        self.sample_seed = sample_seed
        self.universe_size = sum(
            len(chars) ** n for n in range(min_length, max_length + 1)
        )
        self.exhaustive = self.universe_size <= enumerate_cap

    # -- pattern handling ---------------------------------------------------

    def inspect(self, pattern: str) -> InspectedPattern:
        return inspect_pattern(pattern, self.alphabet, length_cap=self.max_length)

    def _hard_scope_problems(self, inspected: InspectedPattern) -> List[str]:
        return [
            "character %r (U+%04X) is outside the declared alphabet: %s"
            % (chr(code), code, reason)
            for code, reason in sorted(inspected.out_of_scope.items())
        ]

    def _soft_scope_problems(self, inspected: InspectedPattern) -> List[str]:
        # Under re.search semantics a short pattern still matches inside a
        # longer key, so only a pattern that needs more characters than any
        # domain key provides is impossible to fire.
        if inspected.min_length > self.max_length:
            return [
                "match length %d..%d exceeds declared max length %d; the rule "
                "cannot fire on any key in the domain"
                % (inspected.min_length, inspected.max_length, self.max_length)
            ]
        return []

    # -- exact counting via subset construction -----------------------------

    def _matched_counts(self, nfa: _NFA) -> Dict[int, int]:
        """Exact number of strings matching (search semantics) per length.

        A DP state is ``(nfa subset, ever_accepted)`` so strings which
        accepted earlier and then continued are still counted at longer
        lengths.  End anchors are only expanded once all chars are consumed.
        """
        start_closure = nfa.closure([nfa.start], at_start=True)
        initial = (start_closure, nfa.accept in start_closure)
        current: Dict[Tuple[FrozenSet[int], bool], int] = {initial: 1}
        counts: Dict[int, int] = {}
        for length in range(1, self.max_length + 1):
            following: Dict[Tuple[FrozenSet[int], bool], int] = {}
            for (stateset, ever), count in current.items():
                for code in self.universe:
                    moved = nfa.move(stateset, code)
                    state = (moved, ever or nfa.accept in moved)
                    following[state] = following.get(state, 0) + count
            matched = 0
            for (stateset, ever), count in following.items():
                if ever or nfa.accept in nfa.closure(stateset, at_end=True):
                    matched += count
            counts[length] = matched
            current = following
        return counts

    # -- deterministic walks for samples ------------------------------------

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(repr((self.sample_seed,) + parts))

    def _walk_unmatched(self, nfas: Sequence[_NFA], length: int) -> Optional[str]:
        """Find one key of exactly *length* that no NFA accepts at any point."""
        statesets = tuple(nfa.closure([nfa.start], at_start=True) for nfa in nfas)
        if any(nfa.accept in stateset for nfa, stateset in zip(nfas, statesets)):
            return None
        rng = self._rng("unmatched", length)
        return self._walk_unmatched_recurse(nfas, statesets, length, [], rng)

    def _walk_unmatched_recurse(
        self,
        nfas: Sequence[_NFA],
        statesets: Tuple[FrozenSet[int], ...],
        remaining: int,
        prefix: List[int],
        rng: random.Random,
    ) -> Optional[str]:
        if remaining == 0:
            for nfa, stateset in zip(nfas, statesets):
                if nfa.accept in nfa.closure(stateset, at_end=True):
                    return None
            return "".join(chr(code) for code in prefix)
        codes = sorted(self.universe)
        rng.shuffle(codes)
        for code in codes:
            next_sets = tuple(
                nfa.move(stateset, code) for nfa, stateset in zip(nfas, statesets)
            )
            if any(nfa.accept in stateset for nfa, stateset in zip(nfas, next_sets)):
                continue
            found = self._walk_unmatched_recurse(
                nfas, next_sets, remaining - 1, prefix + [code], rng
            )
            if found is not None:
                return found
        return None

    def _walk_matched(
        self, nfa: _NFA, length: int, budget: int, tag: str = ""
    ) -> List[str]:
        shortest, longest = nfa.distance_bounds()
        results: List[str] = []
        initial = nfa.closure([nfa.start], at_start=True)
        ever = nfa.accept in initial
        rng = self._rng("matched", tag, length)
        self._walk_matched_recurse(
            nfa,
            initial,
            length,
            [],
            ever,
            results,
            budget,
            shortest,
            longest,
            rng,
        )
        return results

    def _walk_matched_recurse(
        self,
        nfa: _NFA,
        stateset: FrozenSet[int],
        remaining: int,
        prefix: List[int],
        ever_accepted: bool,
        results: List[str],
        budget: int,
        shortest: Dict[int, int],
        longest: Dict[int, int],
        rng: random.Random,
    ) -> None:
        if len(results) >= budget:
            return
        ever_accepted = ever_accepted or nfa.accept in stateset
        if remaining == 0:
            if ever_accepted or nfa.accept in nfa.closure(stateset, at_end=True):
                results.append("".join(chr(code) for code in prefix))
            return
        if not stateset:
            return
        if not ever_accepted:
            lower = min(shortest[state] for state in stateset)
            upper = max(longest[state] for state in stateset)
            if lower > remaining or upper < remaining:
                return
        codes = sorted(self.universe)
        rng.shuffle(codes)
        for code in codes:
            moved = nfa.move(stateset, code)
            self._walk_matched_recurse(
                nfa,
                moved,
                remaining - 1,
                prefix + [code],
                ever_accepted,
                results,
                budget,
                shortest,
                longest,
                rng,
            )
            if len(results) >= budget:
                return

    # -- main entry point ---------------------------------------------------

    def analyze(self, rules: Sequence[Rule]) -> CoverageReport:
        rulebook = Rulebook(rules)
        scope_errors: Dict[str, List[str]] = {}
        usable: List[Rule] = []
        for rule in rulebook.rules:
            try:
                inspected = self.inspect(rule.pattern)
            except UnsupportedPattern as exc:
                scope_errors[rule.name] = [str(exc)]
                continue
            rule.inspected = inspected
            hard = self._hard_scope_problems(inspected)
            soft = self._soft_scope_problems(inspected)
            if hard:
                scope_errors[rule.name] = hard + soft
                continue
            if soft:
                scope_errors[rule.name] = soft
            usable.append(rule)

        sink: Dict[str, RuleCoverage] = {}
        if self.exhaustive:
            per_rule_keys, unmatched = self._collect_exhaustive(rulebook, usable)
            unmatched_existence = {
                length: any(len(key) == length for key in unmatched)
                for length in range(self.min_length, self.max_length + 1)
            }
            unmatched_samples = unmatched[: self.sample_budget]
            self._build_coverage(usable, per_rule_keys, sink, estimated=False)
        else:
            per_rule_keys = self._collect_stratified(usable)
            unmatched_samples, unmatched_existence = self._collect_unmatched(usable)
            self._build_coverage(usable, per_rule_keys, sink, estimated=True)

        return CoverageReport(
            alphabet=self.alphabet,
            min_length=self.min_length,
            max_length=self.max_length,
            universe_size=self.universe_size,
            exhaustive=self.exhaustive,
            scope_errors=scope_errors,
            rule_coverage=sink,
            unmatched_keys=unmatched_samples,
            unmatched_existence_by_length=unmatched_existence,
        )

    # -- exhaustive mode ----------------------------------------------------

    def _all_keys(self) -> Iterable[str]:
        for length in range(self.min_length, self.max_length + 1):
            for product in itertools.product(self.alphabet, repeat=length):
                yield "".join(product)

    def _collect_exhaustive(
        self, rulebook: Rulebook, rules: List[Rule]
    ) -> Tuple[Dict[str, List[str]], List[str]]:
        per_rule_keys: Dict[str, List[str]] = {rule.name: [] for rule in rules}
        unmatched: List[str] = []
        usable = {rule.name for rule in rules}
        for key in self._all_keys():
            matched = [
                rule for rule in rulebook.matching_rules(key) if rule.name in usable
            ]
            if not matched:
                unmatched.append(key)
                continue
            for rule in matched:
                per_rule_keys[rule.name].append(key)
        return per_rule_keys, unmatched

    # -- stratified mode ----------------------------------------------------

    def _collect_stratified(self, rules: List[Rule]) -> Dict[str, List[str]]:
        per_rule_keys: Dict[str, List[str]] = {rule.name: [] for rule in rules}
        for rule in rules:
            nfa = rule.inspected.nfa
            for length in range(self.min_length, self.max_length + 1):
                per_rule_keys[rule.name].extend(
                    self._walk_matched(nfa, length, self.sample_budget, tag=rule.name)
                )
        return per_rule_keys

    def _collect_unmatched(
        self, rules: List[Rule]
    ) -> Tuple[List[str], Dict[int, bool]]:
        samples: List[str] = []
        existence: Dict[int, bool] = {}
        nfas = [rule.inspected.nfa for rule in rules]
        for length in range(self.min_length, self.max_length + 1):
            found = self._walk_unmatched(nfas, length) if nfas else None
            existence[length] = found is not None
            if found is not None:
                samples.append(found)
        return samples[: self.sample_budget], existence

    # -- shared coverage construction --------------------------------------

    def _build_coverage(
        self,
        rules: List[Rule],
        per_rule_keys: Dict[str, List[str]],
        sink: Dict[str, RuleCoverage],
        estimated: bool,
    ) -> None:
        higher_priority = {
            rule.name: [other for other in rules if other.priority < rule.priority]
            for rule in rules
        }
        for rule in rules:
            nfa = rule.inspected.nfa
            exact_counts = self._matched_counts(nfa)
            reachable = sum(
                count
                for length, count in exact_counts.items()
                if self.min_length <= length <= self.max_length
            )
            sample_keys = per_rule_keys[rule.name]
            own_samples: List[str] = []
            shadowed_samples: List[str] = []
            combo_tally: Dict[FrozenSet[str], int] = {}
            shadowed = 0
            for key in sample_keys:
                blockers = frozenset(
                    other.name
                    for other in higher_priority[rule.name]
                    if other.compiled.search(key)
                )
                if blockers:
                    shadowed += 1
                    shadowed_samples.append(key)
                    combo_tally[blockers] = combo_tally.get(blockers, 0) + 1
                else:
                    own_samples.append(key)
            if estimated:
                # Counts stay exact (subset construction above); only the
                # shadowed/own split and the combos are estimated from samples.
                if sample_keys:
                    shadowed_count = round(reachable * shadowed / len(sample_keys))
                else:
                    shadowed_count = 0
                own_count = reachable - shadowed_count
                fully_shadowed = bool(sample_keys) and shadowed == len(sample_keys)
            else:
                shadowed_count = shadowed
                own_count = reachable - shadowed
                fully_shadowed = reachable > 0 and shadowed == reachable
            combos = [
                ComboCoverage(
                    combo=tuple(sorted(combo)),
                    key_count=count,
                    share_of_reachable=(count / len(sample_keys))
                    if sample_keys
                    else 0.0,
                )
                for combo, count in sorted(
                    combo_tally.items(), key=lambda item: (-item[1], item[0])
                )
            ]
            sink[rule.name] = RuleCoverage(
                name=rule.name,
                label=rule.label,
                pattern=rule.pattern,
                priority=rule.priority,
                min_length=rule.inspected.min_length,
                max_length=rule.inspected.max_length,
                reachable_count=reachable,
                shadowed_count=shadowed_count,
                own_count=own_count,
                fully_shadowed=fully_shadowed,
                covering_combos=combos,
                sample_own_keys=own_samples[: self.sample_budget],
                sample_shadowed_keys=shadowed_samples[: self.sample_budget],
                estimated=estimated,
            )


# ---------------------------------------------------------------------------
# Demo: procurement contract field rules
# ---------------------------------------------------------------------------

DEMO_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

SUPPORTED_SYNTAX = (
    "literals; character classes [..] and negated [^..]; alternation |; "
    "capturing/non-capturing groups; bounded quantifiers ?, {m,n}; "
    "unbounded * and + capped to the declared max key length; anchors ^ and $"
)
UNSUPPORTED_SYNTAX = (
    "'.' and backslash categories (\\d, \\w, ...); word boundaries \\b; "
    "look-around; backreferences; inline flags"
)


def demo_rules() -> List[Rule]:
    return [
        Rule("part_no", r"PN[A-Z0-9]{4,8}", priority=10, label="料号"),
        Rule("model", r"M[A-Z]{2,5}[0-9]{1,3}", priority=20, label="型号"),
        Rule("batch", r"B[A-Z0-9]{3,6}", priority=30, label="批号"),
        Rule("lot_new", r"[A-Z]{2}[0-9]{4,6}", priority=40, label="批号(新规则)"),
    ]


def main() -> None:
    print("=" * 78)
    print("SUPPORTED PATTERN SUBSET (everything else is reported, never guessed)")
    print("=" * 78)
    print("  in scope : %s" % SUPPORTED_SYNTAX)
    print("  rejected : %s" % UNSUPPORTED_SYNTAX)

    print()
    print("=" * 78)
    print("SCENARIO 1 - LARGE DECLARED DOMAIN (A-Z0-9, length 4..8, stratified)")
    print("=" * 78)
    analyzer = CoverageAnalyzer(
        DEMO_ALPHABET, min_length=4, max_length=8, sample_budget=60
    )
    report = analyzer.analyze(demo_rules())
    print(report.to_text())

    print()
    print("=" * 78)
    print("SCENARIO 2 - SMALL DOMAIN, EXHAUSTIVE: EXACT COVERING COMBINATIONS")
    print("=" * 78)
    small_rules = [
        Rule("part_no", r"^A", priority=10, label="料号"),
        Rule("model", r"B$", priority=20, label="型号"),
        Rule("batch", r"AB", priority=30, label="批号"),
        Rule("lot_new", r"[AB]{2,4}", priority=40, label="批号(新规则)"),
    ]
    small = CoverageAnalyzer("AB", min_length=1, max_length=4)
    print(small.analyze(small_rules).to_text())

    print()
    print("=" * 78)
    print("SCENARIO 3 - A LEGACY CATCH-ALL RULE FULLY SHADOWS EVERYTHING LATER")
    print("=" * 78)
    legacy = [Rule("legacy_catchall", r"[AB]{1,4}", priority=1)] + small_rules
    print(small.analyze(legacy).to_text())

    print()
    print("=" * 78)
    print("OUT-OF-SCOPE PATTERN EXAMPLE")
    print("=" * 78)
    scoped = analyzer.inspect(r"PN-[a-z]{2}[0-9]{2}")
    for problem in analyzer._hard_scope_problems(scoped):
        print("  - %s" % problem)

    print()
    print("=" * 78)
    print("UNSUPPORTED SYNTAX EXAMPLES")
    print("=" * 78)
    for pattern in (r"A.B", r"A(?=B)", r"(A)\1", r"\d+", r"\bA\b"):
        try:
            analyzer.inspect(pattern)
        except UnsupportedPattern as exc:
            print("  - %-8s -> %s" % (pattern, exc))

    print()
    print("=" * 78)
    print("UNBOUNDED QUANTIFIER WITHOUT A DECLARED LENGTH CAP")
    print("=" * 78)
    try:
        inspect_pattern(r"A*", DEMO_ALPHABET)
    except UnsupportedPattern as exc:
        print("  - %s" % exc)
    print("  (inside CoverageAnalyzer the declared max length supplies the cap,")
    print("   so patterns such as 'B[A-Z0-9]*' are analyzable within the domain)")


def load_config(config: Dict[str, Any]) -> Tuple[CoverageAnalyzer, List[Rule]]:
    """Build an analyzer and rules from a plain JSON-compatible mapping."""
    analyzer = CoverageAnalyzer(
        config["alphabet"],
        min_length=config["min_length"],
        max_length=config["max_length"],
        enumerate_cap=config.get("enumerate_cap", 300_000),
        sample_budget=config.get("sample_budget", 25),
        sample_seed=config.get("sample_seed", 0),
    )
    rules = [
        Rule(
            name=entry["name"],
            pattern=entry["pattern"],
            priority=entry.get("priority"),
            label=entry.get("label", ""),
        )
        for entry in config["rules"]
    ]
    return analyzer, rules


def cli(argv: Optional[Sequence[str]] = None) -> int:
    """``python field_rules.py [--json] [config.json | -]`` (default: demo)."""
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = False
    if "--json" in args:
        as_json = True
        args.remove("--json")
    if not args:
        main()
        return 0
    source = args[0]
    raw = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    try:
        config = json.loads(raw)
        analyzer, rules = load_config(config)
        report = analyzer.analyze(rules)
    except (KeyError, ValueError, OSError) as exc:
        print("invalid configuration: %s" % exc, file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
