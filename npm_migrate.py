#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migrate npm range conditions to the restricted SimpleSpec comparison grammar.

Context
-------
The new source contract expresses conditions in npm range syntax (``NpmSpec``).
A fleet of legacy collectors only understands a frozen whitelist of SimpleSpec
comparison forms::

    <, <=, ==, >=, >, !=        (comma-joined conjunction, full x.y.z targets)

That whitelist cannot be relaxed: no ``||`` unions, no ``^`` / ``~`` / ``~=``
prefixes, no partial wildcards on the collector side.

This tool, for every contract condition:

1. Produces an equivalent SimpleSpec expression when one exists
   (status ``exact``).
2. Otherwise, if a frozen artifact manifest is supplied, checks whether the
   equivalence holds *restricted to that manifest* (status ``manifest-only``)
   -- a one-shot, limited downgrade. Such results are only valid for that
   exact manifest and are flagged as such.
3. Otherwise reports the condition as not accurately expressible
   (``divergent`` / ``not-expressible``) and prints concrete witness versions
   on both ends: versions matched by the npm condition but not by the SimpleSpec
   candidate, and vice versa.

Equivalence is checked empirically against a generated version universe that
deliberately includes zero-major versions (0.0.x / 0.x.y) and prereleases
(alpha / beta / rc lines), not just common final releases.

CLI:
    python npm_migrate.py CONTRACT_FILE [--manifest MANIFEST_FILE] \
        [--md REPORT.md] [--json REPORT.json]
"""

import argparse
import json
import re
import sys

from semantic_version import NpmSpec, SimpleSpec, Version

# The frozen collector whitelist: only these comparison operators may appear
# in the produced SimpleSpec expression.
ALLOWED_SIMPLE_OPS = ('<', '<=', '==', '>=', '>', '!=')

NPM_BLOCK_RE = NpmSpec.Parser.NPM_SPEC_BLOCK

# Baseline version grid: zero-major lines and common release lines, so that
# verification never relies on "common final releases" only.
BASE_GRID = [
    (0, 0, 0), (0, 0, 1), (0, 0, 2), (0, 0, 3), (0, 0, 4),
    (0, 1, 0), (0, 1, 1), (0, 2, 0), (0, 2, 3), (0, 2, 4), (0, 3, 0),
    (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 2, 0), (1, 2, 3), (1, 2, 4),
    (1, 3, 0), (1, 5, 0), (2, 0, 0), (2, 0, 1), (2, 3, 4), (3, 0, 0),
]

DEFAULT_PRERELEASES = [
    (), ('alpha',), ('alpha', '1'), ('beta',), ('beta', '2'),
    ('rc', '1'), ('rc', '2'),
]

VERSION_TOKEN_RE = re.compile(
    r'(\d+)(?:\.(\d+))?(?:\.(\d+))?'
    r'(?:-([0-9A-Za-z.-]+))?'
    r'(?:\+([0-9A-Za-z.-]+))?'
)


class Untranslatable(Exception):
    """The npm condition has no candidate in the restricted SimpleSpec grammar."""


# ---------------------------------------------------------------------------
# Translation: npm range -> restricted SimpleSpec conjunction
# ---------------------------------------------------------------------------

def _fmt(version, keep_build=False):
    text = '%d.%d.%d' % (version.major, version.minor, version.patch)
    if version.prerelease:
        text += '-' + '.'.join(version.prerelease)
    if keep_build and version.build:
        text += '+' + '.'.join(version.build)
    return text


def _translate_block(block):
    """Translate one npm comparator to a list of restricted SimpleSpec blocks.

    Mirrors semantic_version.NpmSpec.Parser.parse_simple so that the emitted
    bounds are exactly the ones the npm clause uses (including zero-major
    caret rules and prerelease targets).
    """
    match = NPM_BLOCK_RE.match(block)
    if match is None:
        raise Untranslatable('unparseable npm comparator %r' % block)

    op, major_t, minor_t, patch_t, prerel, build = match.groups()
    op = {'': '='}.get(op, op)

    empty = ('*', 'x', 'X', None)
    major = None if major_t in empty else int(major_t)
    minor = None if minor_t in empty else int(minor_t)
    patch = None if patch_t in empty else int(patch_t)

    if build is not None and op != '=':
        # npm ignores build metadata on non-equality comparators.
        build = None

    if major is None:
        if op in ('=', '>='):
            return ['>=0.0.0']
        raise Untranslatable('wildcard combined with operator %r' % op)

    if (minor is None or patch is None) and (prerel or build):
        raise Untranslatable('partial version with prerelease/build: %r' % block)

    target = Version(
        major=major,
        minor=minor if minor is not None else 0,
        patch=patch if patch is not None else 0,
        prerelease=prerel.split('.') if prerel else (),
        build=build.split('.') if build else (),
    )

    if op == '^':
        # Zero-major aware: ^1.2.3 -> <2.0.0, ^0.2.3 -> <0.3.0, ^0.0.3 -> <0.0.4
        if target.major:
            high = target.truncate().next_major()
        elif target.minor:
            high = target.truncate().next_minor()
        elif minor is None:  # ^0.x
            high = target.truncate().next_major()
        elif patch is None:  # ^0.2.x
            high = target.truncate().next_minor()
        else:  # ^0.0.3
            high = target.truncate().next_patch()
        return ['>=' + _fmt(target), '<' + _fmt(high)]

    elif op == '~':
        if minor is None:  # ~1 -> <2.0.0
            high = target.next_major()
        else:  # ~1.2 / ~1.2.3 -> <1.3.0 (zero-major: ~0.0.3 -> <0.1.0)
            high = target.next_minor()
        return ['>=' + _fmt(target), '<' + _fmt(high)]

    elif op == '=':
        if minor is None:  # 1.x
            low = target.truncate()
            return ['>=' + _fmt(low), '<' + _fmt(low.next_major())]
        elif patch is None:  # 1.2.x
            low = target.truncate()
            return ['>=' + _fmt(low), '<' + _fmt(low.next_minor())]
        return ['==' + _fmt(target, keep_build=True)]

    elif op == '>':
        if minor is None:
            return ['>=' + _fmt(target.truncate().next_major())]
        elif patch is None:
            return ['>=' + _fmt(target.truncate().next_minor())]
        return ['>' + _fmt(target)]

    elif op == '>=':
        return ['>=' + _fmt(target)]

    elif op == '<':
        return ['<' + _fmt(target)]

    else:
        assert op == '<='
        if minor is None:
            return ['<' + _fmt(target.truncate().next_major())]
        elif patch is None:
            return ['<' + _fmt(target.truncate().next_minor())]
        return ['<=' + _fmt(target)]


def _translate_group(group):
    """Translate one npm comparator set (no ``||``) to a SimpleSpec expression."""
    group = group.strip()
    if not group:
        return '>=0.0.0'
    blocks = []
    if ' - ' in group:
        low, high = group.split(' - ', 2)
        blocks += _translate_block('>=' + low.strip())
        blocks += _translate_block('<=' + high.strip())
    else:
        for block in group.split(' '):
            blocks += _translate_block(block)
    return ','.join(blocks)


def translate_npm(expression):
    """Translate an npm range to the restricted SimpleSpec grammar.

    Raises Untranslatable when no conjunction of whitelisted comparisons can
    be produced (e.g. ``||`` unions).
    """
    groups = expression.split('||')
    if len(groups) > 1:
        raise Untranslatable(
            "npm '||' union cannot be expressed as a SimpleSpec conjunction")
    return _translate_group(groups[0])


def check_whitelist(simple_expression):
    """Ensure the produced expression only uses whitelisted comparison forms."""
    for block in simple_expression.split(','):
        match = re.match(r'^(<|<=|==|>=|>|!=)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$', block)
        if match is None or match.group(1) not in ALLOWED_SIMPLE_OPS:
            return False
    return True


def convex_hull(expression):
    """Best-effort convex hull of a ``||`` union, as a SimpleSpec conjunction.

    Used only to produce witnesses for not-expressible conditions; the hull is
    NOT a valid migration product.
    """
    lowers, uppers = [], []
    for group in expression.split('||'):
        try:
            simple = _translate_group(group)
        except Untranslatable:
            return None
        for block in simple.split(','):
            match = re.match(r'^(>=|<=|>|<|==|!=)(.+)$', block)
            if match is None:
                return None
            op, target = match.groups()
            if op == '!=':
                return None
            if op in ('>=', '>'):
                lowers.append((Version(target), op))
            elif op in ('<=', '<'):
                uppers.append((Version(target), op))
            else:  # ==
                lowers.append((Version(target), '>='))
                uppers.append((Version(target), '<='))
    if not lowers and not uppers:
        return None
    parts = []
    if lowers:
        # Hull = widest coverage: loosest (smallest) lower bound.
        version, op = min(lowers, key=lambda item: (item[0], item[1] == '>'))
        parts.append('%s%s' % (op, version))
    if uppers:
        # Loosest (largest) upper bound.
        version, op = max(uppers, key=lambda item: (item[0], item[1] == '<='))
        parts.append('%s%s' % (op, version))
    if lowers and uppers:
        low = min(v for v, _ in lowers)
        high = max(v for v, _ in uppers)
        if low > high:
            return None
    return ','.join(parts)


# ---------------------------------------------------------------------------
# Verification universe & witnesses
# ---------------------------------------------------------------------------

def _extract_tokens(*expressions):
    """Pull (major, minor, patch, prerelease, build) tokens out of expressions."""
    triples, prerels, builds = set(), set(), set()
    for expression in expressions:
        if not expression:
            continue
        for match in VERSION_TOKEN_RE.finditer(expression):
            major, minor, patch, prerel, build = match.groups()
            triple = (int(major), int(minor or 0), int(patch or 0))
            triples.add(triple)
            if prerel:
                prerels.add(tuple(prerel.split('.')))
            if build:
                builds.add((triple, tuple(build.split('.'))))
    return triples, prerels, builds


def _bumped(prerel):
    """A neighbouring prerelease tag, to probe prerelease boundaries."""
    if prerel and prerel[-1].isdigit():
        return prerel[:-1] + (str(int(prerel[-1]) + 1),)
    return prerel + ('1',)


def build_universe(expressions, manifest=()):
    """Build the verification universe: zero-major lines, releases, prereleases.

    The universe combines a baseline grid with the neighbourhood (+-1 on each
    component) of every version mentioned in the contract or the candidates,
    crossed with common and contract-specific prerelease tags.
    """
    triples, prerels, builds = _extract_tokens(*expressions)

    bases = set(BASE_GRID) | triples
    for major, minor, patch in triples:
        for dm in (-1, 0, 1):
            for dp in (-1, 0, 1):
                if major + dm >= 0 and patch + dp >= 0:
                    bases.add((major + dm, minor, patch + dp))
                if major + dm >= 0 and minor + dp >= 0:
                    bases.add((major + dm, minor + dp, patch))
        bases.add((major, minor, patch + 1))
        bases.add((major, minor + 1, 0))
        bases.add((major + 1, 0, 0))

    tags = set(DEFAULT_PRERELEASES) | prerels
    for prerel in prerels:
        tags.add(_bumped(prerel))

    universe = set()
    for major, minor, patch in bases:
        for tag in tags:
            universe.add(Version(
                major=major, minor=minor, patch=patch, prerelease=tag))
    for (major, minor, patch), build in builds:
        universe.add(Version(
            major=major, minor=minor, patch=patch, build=build))
        universe.add(Version(
            major=major, minor=minor, patch=patch, build=('other',)))
    for version in manifest or ():
        universe.add(Version(version))
    return sorted(universe, key=lambda v: v.precedence_key)


def find_witnesses(npm_spec, simple_spec, versions):
    """Split `versions` into npm-only and simple-only matches."""
    npm_only, simple_only = [], []
    for version in versions:
        in_npm = npm_spec.match(version)
        in_simple = simple_spec.match(version)
        if in_npm and not in_simple:
            npm_only.append(str(version))
        elif in_simple and not in_npm:
            simple_only.append(str(version))
    return npm_only, simple_only


# ---------------------------------------------------------------------------
# Per-condition analysis
# ---------------------------------------------------------------------------

STATUS_EXACT = 'exact'
STATUS_MANIFEST_ONLY = 'manifest-only'
STATUS_DIVERGENT = 'divergent'
STATUS_NOT_EXPRESSIBLE = 'not-expressible'
STATUS_INVALID = 'invalid'


def analyze(expression, manifest=None, universe=None):
    """Analyse one npm contract condition.

    Returns a dict with the candidate SimpleSpec expression (if any), the
    migration status, and concrete witness versions on both ends.
    """
    result = {
        'npm': expression,
        'simple': None,
        'status': None,
        'scope': None,
        'reason': None,
        'witnesses': None,
        'manifest_witnesses': None,
    }

    try:
        npm_spec = NpmSpec(expression)
    except ValueError as exc:
        result.update(status=STATUS_INVALID, reason=str(exc))
        return result

    if universe is None:
        universe = build_universe([expression], manifest)

    try:
        simple_expression = translate_npm(expression)
    except Untranslatable as exc:
        result['status'] = STATUS_NOT_EXPRESSIBLE
        result['reason'] = str(exc)
        hull = convex_hull(expression)
        if hull is not None:
            result['hull'] = hull
            npm_only, hull_only = find_witnesses(
                npm_spec, SimpleSpec(hull), universe)
            result['witnesses'] = {'npm_only': npm_only, 'simple_only': hull_only}
        else:
            matched = [str(v) for v in universe if npm_spec.match(v)]
            result['witnesses'] = {'npm_only': matched[:5], 'simple_only': []}
        return result

    assert check_whitelist(simple_expression), simple_expression
    result['simple'] = simple_expression
    simple_spec = SimpleSpec(simple_expression)

    npm_only, simple_only = find_witnesses(npm_spec, simple_spec, universe)
    result['witnesses'] = {'npm_only': npm_only, 'simple_only': simple_only}

    if not npm_only and not simple_only:
        result['status'] = STATUS_EXACT
        result['scope'] = 'global'
        return result

    if manifest is not None:
        manifest_versions = [Version(v) for v in manifest]
        m_npm_only, m_simple_only = find_witnesses(
            npm_spec, simple_spec, manifest_versions)
        result['manifest_witnesses'] = {
            'npm_only': m_npm_only, 'simple_only': m_simple_only}
        if not m_npm_only and not m_simple_only:
            result['status'] = STATUS_MANIFEST_ONLY
            result['scope'] = 'manifest'
            return result

    result['status'] = STATUS_DIVERGENT
    result['scope'] = 'none'
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    STATUS_EXACT: '全局等价 (exact)',
    STATUS_MANIFEST_ONLY: '仅冻结清单内等价 (manifest-only)',
    STATUS_DIVERGENT: '不可准确表达 (divergent)',
    STATUS_NOT_EXPRESSIBLE: '无法用合取表达 (not-expressible)',
    STATUS_INVALID: '非法条件 (invalid)',
}


def _fmt_witnesses(witnesses, limit=3):
    if not witnesses:
        return '-'
    parts = []
    for side in ('npm_only', 'simple_only'):
        values = witnesses.get(side) or []
        label = 'npm 侧' if side == 'npm_only' else 'simple 侧'
        if values:
            shown = ', '.join('`%s`' % v for v in values[:limit])
            if len(values) > limit:
                shown += ' 等 %d 个' % len(values)
            parts.append('%s %s' % (label, shown))
        else:
            parts.append('%s 无' % label)
    return '; '.join(parts)


def render_markdown(results, manifest, universe_size, contract_path, manifest_path):
    lines = []
    lines.append('# NPM → SimpleSpec 迁移报告')
    lines.append('')
    lines.append('- 来源合同: `%s`（%d 条 npm 范围条件）' % (contract_path, len(results)))
    lines.append('- 目标语法: SimpleSpec 比较式白名单 `%s`，逗号合取，完整 x.y.z 目标版本'
                 % ', '.join(ALLOWED_SIMPLE_OPS))
    if manifest is not None:
        lines.append('- 冻结制品清单: `%s`（%d 个版本）' % (manifest_path, len(manifest)))
    else:
        lines.append('- 冻结制品清单: 未提供（未启用有限降级）')
    lines.append('- 校验宇宙: %d 个版本（含零主版本 0.0.x/0.x.y 与 alpha/beta/rc 预发布版）'
                 % universe_size)
    lines.append('')
    lines.append('## 汇总')
    lines.append('')
    lines.append('| 合同条件 (npm) | 迁移产物 (SimpleSpec) | 结论 | 全局见证 (npm 侧 / simple 侧) |')
    lines.append('| --- | --- | --- | --- |')
    for result in results:
        simple = '`%s`' % result['simple'] if result['simple'] else '—'
        lines.append('| `%s` | %s | %s | %s |' % (
            result['npm'], simple, STATUS_LABELS[result['status']],
            _fmt_witnesses(result['witnesses'])))
    lines.append('')
    lines.append('## 逐项详情')
    for index, result in enumerate(results, 1):
        lines.append('')
        lines.append('### %d. `%s`' % (index, result['npm']))
        lines.append('')
        if result['simple']:
            lines.append('- 迁移产物: `%s`' % result['simple'])
        if result.get('hull'):
            lines.append('- 参考凸包（非迁移产物）: `%s`' % result['hull'])
        lines.append('- 结论: **%s**' % STATUS_LABELS[result['status']])
        if result['reason']:
            lines.append('- 原因: %s' % result['reason'])
        if result['status'] == STATUS_EXACT:
            lines.append('- 声明: 在含零主版本与预发布版的校验宇宙上与 npm 条件完全一致，可无条件迁移。')
        elif result['status'] == STATUS_MANIFEST_ONLY:
            lines.append('- 声明: **该等价仅对冻结制品清单（%d 个版本）成立**，'
                         '是允许的一次性有限降级；对清单外版本（尤其预发布版）不成立，'
                         '禁止外推到其他制品集。' % len(manifest))
            lines.append('- 全局见证（清单外反例）: %s' % _fmt_witnesses(result['witnesses']))
        elif result['status'] == STATUS_DIVERGENT:
            lines.append('- 两端具体版本见证: %s' % _fmt_witnesses(result['witnesses']))
            if result.get('manifest_witnesses'):
                lines.append('- 冻结清单内见证: %s' % _fmt_witnesses(result['manifest_witnesses']))
        elif result['status'] == STATUS_NOT_EXPRESSIBLE:
            lines.append('- 两端具体版本见证（相对参考凸包）: %s' % _fmt_witnesses(result['witnesses']))
    lines.append('')
    lines.append('---')
    lines.append('复现: `python npm_migrate.py %s%s --md MIGRATION_REPORT.md --json migration_results.json`'
                 % (contract_path, ' --manifest %s' % manifest_path if manifest_path else ''))
    lines.append('')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_lines(path):
    with open(path) as stream:
        return [
            line.strip() for line in stream
            if line.strip() and not line.startswith('#')
        ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('contract', help='File with one npm range condition per line')
    parser.add_argument('--manifest', help='Frozen artifact manifest (one version per line)')
    parser.add_argument('--md', help='Write a Markdown migration report here')
    parser.add_argument('--json', dest='json_path', help='Write machine-readable results here')
    args = parser.parse_args(argv)

    conditions = _read_lines(args.contract)
    manifest = _read_lines(args.manifest) if args.manifest else None

    universe = build_universe(conditions, manifest)
    results = [analyze(condition, manifest, universe) for condition in conditions]

    counts = {}
    for result in results:
        counts[result['status']] = counts.get(result['status'], 0) + 1
    print('Analysed %d conditions against a universe of %d versions:'
          % (len(results), len(universe)))
    for status in (STATUS_EXACT, STATUS_MANIFEST_ONLY, STATUS_DIVERGENT,
                   STATUS_NOT_EXPRESSIBLE, STATUS_INVALID):
        if status in counts:
            print('  %-16s %d' % (STATUS_LABELS[status], counts[status]))

    if args.md:
        with open(args.md, 'w') as stream:
            stream.write(render_markdown(
                results, manifest, len(universe), args.contract, args.manifest))
        print('Markdown report written to %s' % args.md)
    if args.json_path:
        with open(args.json_path, 'w') as stream:
            json.dump(results, stream, indent=2, ensure_ascii=False)
        print('JSON results written to %s' % args.json_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
