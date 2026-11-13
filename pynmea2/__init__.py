# pylint: disable=missing-docstring
# pylint: disable=wildcard-import
# pylint: disable=invalid-name

from ._version import __version__
version = __version__

from .nmea import NMEASentence, ProprietarySentence, QuerySentence
from .nmea import ChecksumError, ParseError, SentenceTypeError

parse = NMEASentence.parse

from .types import *

from .satellites import (
    Satellite,
    SignalObservation,
    GSVRound,
    GSARound,
    SatelliteReconciler,
    resolve_constellation,
    extended_constellation,
    signal_name,
    signal_constellation,
    TALKER_CONSTELLATIONS,
    EXTENDED_SATELLITE_RANGES,
    SIGNAL_NAMES,
)

from .stream import NMEAStreamReader
from .nmea_file import NMEAFile

