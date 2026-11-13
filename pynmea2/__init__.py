# pylint: disable=missing-docstring
# pylint: disable=wildcard-import
# pylint: disable=invalid-name

from ._version import __version__
version = __version__

from .nmea import NMEASentence, ProprietarySentence, QuerySentence
from .nmea import ChecksumError, ParseError, SentenceTypeError

parse = NMEASentence.parse

from .types import *

from .route_xte import (
    Leg,
    LegDeviation,
    CrossTrackObservation,
    RouteTrack,
    travel_signed,
    route_signed,
    side_of,
    extract_cross_track,
)

from .stream import NMEAStreamReader
from .nmea_file import NMEAFile

