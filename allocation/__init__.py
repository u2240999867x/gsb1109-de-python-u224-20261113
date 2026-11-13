"""Collector-to-source allocation planning.

The package computes the maximum number of data-contract sources that can
still be covered by the declared fleet of collectors, respecting:

* NpmSpec compatibility (including the npm pre-release rules),
* each collector's declared ``can_read`` source allow-list,
* each collector's concurrency capacity,
* immutable locked assignments.
"""

from .flow import Dinic
from .loader import LoadError, load_input, load_json
from .model import Assignment, Collector, Plan, Source, StoppedSource
from .planner import InvalidLockError, plan_allocation

__all__ = [
    'Assignment',
    'Collector',
    'Dinic',
    'InvalidLockError',
    'LoadError',
    'Plan',
    'Source',
    'StoppedSource',
    'load_input',
    'load_json',
    'plan_allocation',
]
