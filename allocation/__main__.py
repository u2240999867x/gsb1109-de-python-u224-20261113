"""Command-line entry point: python -m allocation plan.json."""

import argparse
import json
import sys

from .loader import LoadError, load_json
from .planner import InvalidLockError, plan_allocation


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Compute maximum-coverage collector allocation.',
    )
    parser.add_argument(
        'input', nargs='?',
        help='JSON allocation document (defaults to stdin).',
    )
    parser.add_argument(
        '--pretty', action='store_true',
        help='Pretty-print a human-readable report instead of JSON.',
    )
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, 'r', encoding='utf-8') as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()

    try:
        sources, collectors = load_json(text)
        plan = plan_allocation(sources, collectors)
    except (LoadError, InvalidLockError) as error:
        parser.exit(2, 'error: %s\n' % error)

    if args.pretty:
        _print_report(plan)
    else:
        print(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False))
    return 0


def _print_report(plan):
    print('Covered %d / %d sources' % (
        plan.covered_count, len(plan.sources),
    ))
    print('')
    print('Assignments:')
    for item in plan.assignments:
        flag = ' [locked]' if item.locked else ''
        print('  %s -> %s%s' % (item.source_id, item.collector_id, flag))
    loads = plan.load_by_collector()
    print('')
    print('Collector load:')
    for collector in plan.collectors:
        print('  %s: %d / %d' % (
            collector.id, loads[collector.id], collector.capacity,
        ))
    print('')
    if plan.stopped:
        print('Unavoidable stoppages (%d):' % plan.stopped_count)
        for item in plan.stopped:
            print('  %s (%s): %s' % (
                item.source_id, item.contract_version, item.reason,
            ))
    else:
        print('No stoppages.')


if __name__ == '__main__':
    sys.exit(main())
