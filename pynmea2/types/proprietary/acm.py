# ACME sensors
#
# The ACMFLX environmental sentence keeps the same name, field count and
# checksum across firmware generations, but firmware 2.0 swaps the
# temperature and pressure columns.  Because XOR checksumming is invariant
# under a byte permutation the raw message cannot identify the revision on
# its own, so ACMFLX is a VersionedProprietarySentence resolved through the
# firmware revision registry.
#
# Sentences use the comma form "$PACM,FLX,...", so the leading data element
# is empty and the subtype lives at data[1] (the same convention as TNL/VTX).

from ... import nmea
from ...revision import VersionedProprietarySentence


class ACM(nmea.ProprietarySentence):
    sentence_types = {}

    def __new__(_cls, manufacturer, data):
        name = manufacturer + data[1]
        cls = _cls.sentence_types.get(name, _cls)
        return super(ACM, cls).__new__(cls)

    def __init__(self, manufacturer, data):
        self.sentence_type = manufacturer + (data[1] if len(data) > 1 else '')
        super(ACM, self).__init__(manufacturer, data)


class ACMFLX(ACM, VersionedProprietarySentence):
    """ACME environmental payload.

    firmware 1.x:  $PACM,FLX,<seq>,<temperature>,<pressure>,<status>
    firmware 2.x:  $PACM,FLX,<seq>,<pressure>,<temperature>,<status>
    """

    sentence_data_index = 1

    revisions = {
        'fw1': (
            ('Empty', '_'),
            ('Subtype', 'subtype'),
            ('Sequence', 'sequence', int),
            ('Temperature (C)', 'temperature', float),
            ('Pressure (hPa)', 'pressure', float),
            ('Status', 'status'),
        ),
        'fw2': (
            ('Empty', '_'),
            ('Subtype', 'subtype'),
            ('Sequence', 'sequence', int),
            ('Pressure (hPa)', 'pressure', float),
            ('Temperature (C)', 'temperature', float),
            ('Status', 'status'),
        ),
    }
