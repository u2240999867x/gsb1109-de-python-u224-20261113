# -*- coding: utf-8 -*-
"""Compare the accepted-version sets of two collection specs.

Usage:
    python spec_range_compare.py OLD_SPEC NEW_SPEC [--syntax simple|npm|auto]
    python spec_range_compare.py --demo

The comparison is performed entirely through this library's public entry
points (BaseSpec.parse / SimpleSpec / NpmSpec / semantic_version.match), so
any printed witness is directly verifiable through the existing matching API.
"""

import argparse
import itertools

import semantic_version
from semantic_version import base


SIMPLE = 'simple'
NPM = 'npm'


def detect_syntax(text):
    # Only the NPM grammar supports top-level disjunction, space-joined
    # conjunctions and hyphen ranges; SimpleSpec only understands comma blocks.
    if '||' in text or ' - ' in text:
        return NPM
    if ' ' in text.strip():
        return NPM
    return SIMPLE


def parse_spec(text, syntax):
    """Parse a spec, returning (spec, error_segment).

    error_segment is None on success; otherwise the exact piece of the
    expression that the chosen grammar rejects.
    """
    cls = base.SimpleSpec if syntax == SIMPLE else base.NpmSpec
    try:
        return cls(text), None
    except ValueError:
        pass

    if syntax == SIMPLE:
        segments = [part.strip() for part in text.split(',')]
        for segment in segments:
            try:
                base.SimpleSpec(segment)
            except ValueError:
                return None, segment
    else:
        for group in text.split('||'):
            group = group.strip()
            pieces = (group.split(' - ', 1) if ' - ' in group
                      else group.split(' '))
            for piece in pieces:
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    base.NpmSpec(piece)
                except ValueError:
                    return None, piece
    return None, text


def grammar_issues(text, syntax):
    """Flag constructs the chosen grammar cannot represent."""
    issues = []
    if syntax == SIMPLE:
        if '||' in text:
            issues.append("disjunction '||' is NPM-only; SimpleSpec joins "
                          "clauses with ',' (intersection, not union)")
        if ' - ' in text:
            issues.append("hyphen range ' - ' is NPM-only")
        if ' ' in text.strip():
            issues.append("space-separated conjunction is NPM-only; use ','")
        for segment in text.split(','):
            segment = segment.strip()
            if '+' in segment.split('-', 1)[-1] and not segment.startswith(('==', '!=')):
                issues.append("build metadata in %r has no ordering; only "
                              "==/!= may carry '+build'" % segment)
    else:
        for group in text.split('||'):
            for piece in group.split():
                if piece.startswith('!='):
                    issues.append("'%s': NPM grammar has no '!=' operator" % piece)
                if '+' in piece and not piece.lstrip('v').startswith(('=',)):
                    issues.append("'%s': NPM ignores build metadata outside =="
                                  % piece)
    return issues


def clause_ranges(clause):
    """Yield every Range matcher inside an AnyOf/AllOf clause tree."""
    if isinstance(clause, base.Range):
        yield clause
    elif isinstance(clause, (base.AnyOf, base.AllOf)):
        for sub in clause.clauses:
            for item in clause_ranges(sub):
                yield item


def neighborhood(target):
    """Versions around one clause target where membership can change."""
    release = base.Version(
        major=target.major, minor=target.minor, patch=target.patch)
    points = {release}
    for d_major, d_minor, d_patch in (
            (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
            (0, 0, 1), (0, 0, -1)):
        major = max(0, release.major + d_major)
        minor = max(0, release.minor + d_minor)
        patch = max(0, release.patch + d_patch)
        points.add(base.Version(major=major, minor=minor, patch=patch))
    for prerelease in (('alpha',), ('rc', '1'), target.prerelease or ()):
        points.add(base.Version(
            major=release.major, minor=release.minor, patch=release.patch,
            prerelease=tuple(prerelease)))
        points.add(base.Version(
            major=release.major, minor=release.minor, patch=release.patch,
            prerelease=tuple(prerelease), build=('1',)))
    return points


def universe(specs=(), limit=3):
    """Bounded grid plus every clause boundary neighborhood.

    All accepted sets here are finite unions of half-open ranges over
    explicit targets, so membership only changes at (or at the same-patch
    prereleases of) a clause target; those points are all included.
    """
    points = set()
    prereleases = [(), ('alpha',), ('alpha', '1'), ('beta',), ('rc', '1'), ('rc', '2')]
    builds = [(), ('1',), ('build', '7')]
    for major, minor, patch in itertools.product(range(limit + 1), repeat=3):
        for prerelease in prereleases:
            for build in builds:
                points.add(base.Version(
                    major=major, minor=minor, patch=patch,
                    prerelease=prerelease, build=build))
    for spec in specs:
        for rng in clause_ranges(spec.clause):
            points |= neighborhood(rng.target)
    return sorted(points, key=lambda v: v.precedence_key)


def compare(old_text, new_text, old_syntax, new_syntax, limit=3):
    old_spec, old_bad = parse_spec(old_text, old_syntax)
    new_spec, new_bad = parse_spec(new_text, new_syntax)
    if old_bad is not None:
        return {'fatal': 'old', 'segment': old_bad,
                'issues': grammar_issues(old_text, old_syntax)}
    if new_bad is not None:
        return {'fatal': 'new', 'segment': new_bad,
                'issues': grammar_issues(new_text, new_syntax)}

    dropped = []   # old accepted, new rejects -> coverage regression
    added = []     # new accepts, old rejected
    for version in universe((old_spec, new_spec), limit):
        in_old = old_spec.match(version)
        in_new = new_spec.match(version)
        if in_old and not in_new:
            dropped.append(version)
        elif in_new and not in_old:
            added.append(version)

    return {
        'fatal': None,
        'old_spec': old_spec,
        'new_spec': new_spec,
        'dropped': dropped,
        'added': added,
    }


def verify_witness(spec_text, syntax, version_text):
    """Exercise every public matching entry point on one candidate."""
    version = base.Version(version_text)
    spec = base.BaseSpec.parse(spec_text, syntax=syntax)
    results = {
        'BaseSpec.parse(...).match': spec.match(version),
        "'in' operator": version in spec,
    }
    if syntax == SIMPLE:
        results['semantic_version.match(str, str)'] = semantic_version.match(
            spec_text, version_text)
    return results


DEMOS = [
    # label, (old_text, old_syntax), (new_text, new_syntax)
    ("equivalent rewrites (no difference expected)",
     ('>=1.0.0,<2.0.0', SIMPLE), ('^1.0.0', SIMPLE)),
    ("natural vs explicit prerelease boundary",
     ('<1.2.3', SIMPLE), ('<1.2.3-', SIMPLE)),
    ("disjunction that drops a branch",
     ('1.2.7 || >=1.2.9 <2.0.0', NPM), ('>=1.2.9 <2.0.0', NPM)),
    ("same text, simple grammar relaxed vs npm same-patch prereleases",
     ('>=1.2.3', SIMPLE), ('>=1.2.3', NPM)),
    ("construct outside the agreed grammar",
     ('>=1.0.0 || <2.0.0', SIMPLE), ('>=1.0.0,<2.0.0', SIMPLE)),
]


def run_demo():
    for label, (old_text, old_syntax), (new_text, new_syntax) in DEMOS:
        print('=' * 72)
        print(label)
        print('  old(%s):' % old_syntax, old_text)
        print('  new(%s):' % new_syntax, new_text)
        result = compare(old_text, new_text, old_syntax, new_syntax)
        if result['fatal']:
            print('  BLOCKED: unparseable segment %r' % result['segment'])
            for issue in result['issues']:
                print('    -', issue)
            continue

        if not result['dropped']:
            print('  result : new set COVERS the old set on the sampled universe')
        else:
            witness = result['dropped'][0]
            print('  result : REGRESSION, %d dropped versions; witness %r'
                  % (len(result['dropped']), str(witness)))
            print('    old entry points:',
                  verify_witness(old_text, old_syntax, str(witness)))
            print('    new entry points:',
                  verify_witness(new_text, new_syntax, str(witness)))
        if result['added']:
            print('  note   : %d versions newly accepted (e.g. %r)'
                  % (len(result['added']), str(result['added'][0])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old', nargs='?')
    parser.add_argument('new', nargs='?')
    parser.add_argument('--syntax', choices=[SIMPLE, NPM, 'auto'],
                        default='auto',
                        help="shared syntax; 'auto' detects per expression")
    parser.add_argument('--limit', type=int, default=3,
                        help='numeric bound per major/minor/patch component')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()

    if args.demo or not (args.old and args.new):
        run_demo()
        return

    if args.syntax == 'auto':
        old_syntax = detect_syntax(args.old)
        new_syntax = detect_syntax(args.new)
    else:
        old_syntax = new_syntax = args.syntax
    print('old syntax:', old_syntax, '| new syntax:', new_syntax)
    result = compare(args.old, args.new, old_syntax, new_syntax, args.limit)
    if result['fatal']:
        print('Cannot compare: segment %r is rejected by the grammar.'
              % result['segment'])
        for issue in result['issues']:
            print('  -', issue)
        return

    sampled = universe((result['old_spec'], result['new_spec']), args.limit)
    print('universe size :', len(sampled))
    print('old accepted  :', sum(1 for v in sampled
                                 if result['old_spec'].match(v)))
    print('new accepted  :', sum(1 for v in sampled
                                 if result['new_spec'].match(v)))
    if not result['dropped']:
        print('COVERS: every sampled version the old config accepts, the new one accepts.')
    else:
        witness = result['dropped'][0]
        print('REGRESSION witness (old=True, new=False):', str(witness))
        print('  old:', verify_witness(args.old, old_syntax, str(witness)))
        print('  new:', verify_witness(args.new, new_syntax, str(witness)))
    if result['added']:
        print('newly accepted by new config, e.g.:',
              ', '.join(str(v) for v in result['added'][:5]))


if __name__ == '__main__':
    main()
