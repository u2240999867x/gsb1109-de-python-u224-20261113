class LASDataError(Exception):

    """Error during reading of numerical data from LAS file."""

    pass


class LASHeaderError(Exception):

    """Error during reading of header data from LAS file."""

    pass


class LASUnknownUnitError(Exception):

    """Error of unknown unit in LAS file."""

    pass


class LASParameterHandoffError(Exception):

    """Raised when the parameter applicable at a depth cannot be determined.

    This is the situation where taking the last value sharing a mnemonic
    would hide a missing or ambiguous handover (e.g. a gap between logging
    batches, or repeated settings inside one batch with no segment
    declaration saying which one applies).

    Attributes:
        mnemonic (str): parameter mnemonic the lookup was attempted for
        occurrences (list): the ParameterOccurrence objects which were
            considered, in source order
        reason (str): short machine-readable code, one of ``no_coverage``,
            ``coverage_gap``, ``ambiguous_occurrence`` or ``not_found``
        missing (str): explanation of which handover or declaration is
            required to resolve the lookup

    """

    def __init__(self, message, mnemonic=None, occurrences=None,
                 reason=None, missing=None):
        super(LASParameterHandoffError, self).__init__(message)
        self.mnemonic = mnemonic
        self.occurrences = occurrences or []
        self.reason = reason
        self.missing = missing
