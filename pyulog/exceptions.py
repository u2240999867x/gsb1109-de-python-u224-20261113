""" Exceptions raised by pyulog """


class ULogException(Exception):
    """
    Raised if a ULog file cannot be parsed at all, e.g. because of an invalid header.
    Corrupt messages are skipped instead, and reported via ULog.file_corruption.
    """


class ULogIncompatibleFlagException(ULogException):
    """
    Raised if a ULog file has an unknown incompatible flag set, i.e. it uses a format
    extension that this version of pyulog cannot parse.
    """


class ULogMergeConflict(ULogException):
    """
    Raised when a supplement contradicts samples that are already archived.

    The existing record is left untouched. ``topic``, ``multi_id`` and
    ``timestamp`` identify the first conflicting sample (chronologically, then
    by topic name and instance).
    """

    def __init__(self, topic, timestamp, multi_id=0, reason=''):
        self.topic = topic
        self.multi_id = multi_id
        self.timestamp = int(timestamp)
        self.reason = reason
        self.conflict_topic = topic
        self.conflict_multi_id = multi_id
        self.conflict_timestamp = int(timestamp)
        self.first_conflict = (topic, multi_id, int(timestamp))
        message = ("Conflicting sample for topic '{}' (multi id {}) at timestamp {}"
                   .format(topic, multi_id, int(timestamp)))
        if reason:
            message += ': ' + reason
        message += '; the archived record was kept unchanged'
        super().__init__(message)


class ULogMergeEvidenceError(ULogException):
    """
    Raised when a file cannot be shown to belong to an existing acquisition.

    This is deliberately separate from :class:`ULogMergeConflict`: identical
    start/end timestamps alone, or a hash that does not match, are not evidence
    that two files are parts of the same acquisition.
    """

