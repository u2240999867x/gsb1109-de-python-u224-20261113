class LASDataError(Exception):

    """Error during reading of numerical data from LAS file."""

    pass


class LASHeaderError(Exception):

    """Error during reading of header data from LAS file."""

    pass


class LASUnknownUnitError(Exception):

    """Error of unknown unit in LAS file."""

    pass


class LASRoundTripError(LASDataError):

    """Fixed-width export would lose sampling identity or exceed tolerance.

    The attached ``report`` attribute is a
    :class:`lasio.roundtrip.RoundTripReport` containing the requested width,
    tolerance, identity curves, and a bounded set of conflicting samples.
    """

    def __init__(self, report):
        self.report = report
        super(LASRoundTripError, self).__init__(str(report))


# Aliases kept for callers that use a more explicit spelling.
LASRoundTripCheckError = LASRoundTripError
RoundTripError = LASRoundTripError
