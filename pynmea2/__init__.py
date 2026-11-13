# pylint: disable=missing-docstring
# pylint: disable=wildcard-import
# pylint: disable=invalid-name

from ._version import __version__
version = __version__

from .nmea import NMEASentence, ProprietarySentence, QuerySentence
from .nmea import ChecksumError, ParseError, SentenceTypeError

parse = NMEASentence.parse

from .types import *

from .depth import (
    AuditResult,
    ColumnEvidence,
    DepthAuditError,
    Observation,
    audit_dbt,
    distinguishing_depth,
    label_mapping,
    load_observations,
    parse_dbt,
    standard_mapping,
    swapped_feet_meters_mapping,
)

from .stream import NMEAStreamReader
from .nmea_file import NMEAFile

