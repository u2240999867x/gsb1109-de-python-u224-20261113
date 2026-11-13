class LASDataError(Exception):

    """Error during reading of numerical data from LAS file."""

    pass


class WrappedDataError(LASDataError):

    """A wrapped (~A, WRAP=YES) data section cannot be re-aligned.

    Raised by the wrapped-data repair machinery when a depth step cannot be
    rebuilt unambiguously and no further re-alignment points are available.

    """

    pass


class LASHeaderError(Exception):

    """Error during reading of header data from LAS file."""

    pass


class LASUnknownUnitError(Exception):

    """Error of unknown unit in LAS file."""

    pass
