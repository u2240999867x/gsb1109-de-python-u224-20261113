"""
Collapse tagged bench log messages into alarm events.

The same alarm can be raised by several modules (each ULog logging tag
identifies one module), and every module keeps repeating the same message.
This module turns that stream into events with a start, a repetition count and
a recovery basis. The caller supplies the alarm rules and the time ranges in
which each tag mapping is valid, so no configuration is hard coded here.

Recovery never clears an alarm raised by another module: state is tracked per
(tag, rule). When the first error of an alarm is missing the event is only
marked as already in alarm (see ALREADY_IN_ALARM), the start is never invented.
Every message counted into an event is kept on it, so each repetition can be
traced back to the original log message.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple


ALREADY_IN_ALARM = '已在告警中'


@dataclass
class AlarmRule:
    """
    Definition of one alarm.

    :param alarm_id: stable identifier of the alarm
    :param is_alarm: predicate deciding whether a logged message raises the
           alarm (True) or clears it (False); other rules simply ignore it
    :param description: optional human readable description
    """
    alarm_id: str
    is_alarm: Callable[[str], bool]
    description: str = ''


@dataclass
class TagMapping:
    """
    Maps logging tags (modules) to rule ids during a validity period.

    :param valid_from: inclusive lower bound in microseconds
    :param valid_to: inclusive upper bound in microseconds
    :param tag_to_rules: tag (module) mapped to the applicable rule ids
    """
    valid_from: int
    valid_to: int
    tag_to_rules: Dict[int, List[str]]

    def covers(self, timestamp: int) -> bool:
        """ Return whether the mapping is in effect at the timestamp. """
        return self.valid_from <= timestamp <= self.valid_to


@dataclass
class TaggedMessage:
    """
    One tagged log message, kept so events can trace back to their source.

    :param original: the underlying log object (e.g. ULog
           MessageLoggingTagged) when one exists, so every repetition can be
           matched back to the exact message it came from
    """
    timestamp: int
    tag: int
    log_level: int
    message: str
    original: Optional[object] = None


@dataclass
class AlarmEvent:
    """
    A single alarm episode raised by one module (tag) for one rule.

    :param alarm_id: matched rule id
    :param tag: logging tag (module) the messages came from
    :param start_timestamp: first error timestamp, or None when the episode
           was already active when the observed log section began
    :param repeat_count: number of repeated alarm messages after the first one
    :param sources: every message counted into the event, in order
    :param recovery: recovery basis message, or None while still active
    :param start_missing: True when the original error was not observed
    """
    alarm_id: str
    tag: int
    start_timestamp: Optional[int]
    repeat_count: int = 0
    sources: List[TaggedMessage] = field(default_factory=list)
    recovery: Optional[TaggedMessage] = None
    start_missing: bool = False

    @property
    def already_in_alarm(self) -> bool:
        """ True when the first error of the episode is missing. """
        return self.start_missing

    @property
    def start_note(self) -> Optional[str]:
        """ Note describing the start; the fixed phrase when it is missing. """
        return ALREADY_IN_ALARM if self.start_missing else None

    @property
    def recovered(self) -> bool:
        """ True once a recovery message has cleared the episode. """
        return self.recovery is not None

    @property
    def end_timestamp(self) -> Optional[int]:
        """ Timestamp of the last message belonging to the event. """
        if self.recovery is not None:
            return self.recovery.timestamp
        if self.sources:
            return self.sources[-1].timestamp
        return None

    def add_alarm(self, message: TaggedMessage) -> None:
        """ Record an alarm message, opening or extending the episode. """
        if self.start_timestamp is None and not self.start_missing:
            # The first observed error opens a fresh episode.
            self.start_timestamp = message.timestamp
        self.sources.append(message)
        # With a known start the opening message is not a repetition; when the
        # start is missing every observed message is a repetition.
        self.repeat_count = len(self.sources) - (0 if self.start_missing else 1)

    def recover(self, message: TaggedMessage) -> bool:
        """
        Clear the episode with a recovery message.

        :return: True when an active episode was cleared, False when it had
                 already been recovered
        """
        if self.recovery is not None:
            return False
        self.recovery = message
        return True


class AlarmEventBuilder:
    """
    Build alarm events from tagged log messages.

    State is kept independently for every (tag, rule) pair, so a recovery from
    one module never clears the alarm of another module.
    """

    def __init__(self, rules: Sequence[AlarmRule],
                 tag_mappings: Sequence[TagMapping]):
        self._rules = {rule.alarm_id: rule for rule in rules}
        self._tag_mappings = list(tag_mappings)
        # key = (tag, rule_id) -> open AlarmEvent
        self._active: Dict[Tuple[int, str], AlarmEvent] = {}
        self._events: List[AlarmEvent] = []

    @property
    def events(self) -> List[AlarmEvent]:
        """ All events produced so far, in order of their first appearance. """
        return list(self._events)

    def mark_active(self, tag: int, rule_id: str) -> AlarmEvent:
        """
        Record that an alarm is already active before the processed section.

        The original error is not available, so the resulting event stays
        without a start timestamp and is flagged as already in alarm instead
        of inventing one. It can still collect repetitions and a recovery.
        """
        if rule_id not in self._rules:
            raise KeyError('Unknown alarm rule id: {}'.format(rule_id))
        key = (tag, rule_id)
        event = self._active.get(key)
        if event is None:
            event = AlarmEvent(alarm_id=rule_id, tag=tag,
                               start_timestamp=None, start_missing=True)
            self._active[key] = event
            self._events.append(event)
        return event

    def build(self, tagged_messages: dict) -> List[AlarmEvent]:
        """
        Build the events from the tagged messages of a ULog.

        :param tagged_messages: dict tag -> list of messages exposing
               timestamp, tag, log_level and message attributes (e.g. the
               dict returned by ULog.logged_messages_tagged)
        :return: the completed and still active events, in chronological order
        """
        for tagged in self._flatten(tagged_messages):
            self._process(tagged)
        return self.events

    def build_from_ulog(self, ulog) -> List[AlarmEvent]:
        """ Build events directly from a parsed ULog object. """
        return self.build(ulog.logged_messages_tagged)

    @staticmethod
    def _flatten(tagged_messages: dict) -> List[TaggedMessage]:
        stream: List[TaggedMessage] = []
        for tag, messages in tagged_messages.items():
            for msg in messages:
                stream.append(TaggedMessage(msg.timestamp, tag,
                                            msg.log_level, msg.message,
                                            original=msg))
        return sorted(stream, key=lambda tagged: tagged.timestamp)

    def _process(self, tagged: TaggedMessage) -> None:
        for rule_id in self._matching_rule_ids(tagged):
            rule = self._rules[rule_id]
            key = (tagged.tag, rule_id)
            if rule.is_alarm(tagged.message):
                self._record_alarm(key, rule_id, tagged)
            else:
                self._record_recovery(key, tagged)

    def _matching_rule_ids(self, tagged: TaggedMessage) -> List[str]:
        for mapping in self._tag_mappings:
            if not mapping.covers(tagged.timestamp):
                continue
            rule_ids = mapping.tag_to_rules.get(tagged.tag, [])
            # The first covering mapping wins; unknown rule ids are ignored.
            return [rule_id for rule_id in rule_ids
                    if rule_id in self._rules]
        return []

    def _record_alarm(self, key: Tuple[int, str], rule_id: str,
                      tagged: TaggedMessage) -> None:
        event = self._active.get(key)
        if event is None:
            event = AlarmEvent(alarm_id=rule_id, tag=tagged.tag,
                               start_timestamp=None)
            self._active[key] = event
            self._events.append(event)
        event.add_alarm(tagged)

    def _record_recovery(self, key: Tuple[int, str],
                         tagged: TaggedMessage) -> None:
        event = self._active.pop(key, None)
        if event is not None:
            event.recover(tagged)


def build_alarm_events(tagged_messages: dict, rules: Sequence[AlarmRule],
                       tag_mappings: Sequence[TagMapping]) -> List[AlarmEvent]:
    """
    Convenience wrapper around :class:`AlarmEventBuilder`.

    :param tagged_messages: dict tag -> list of tagged log messages
    :param rules: alarm rules used to classify the messages
    :param tag_mappings: time bounded tag (module) to rule id mappings
    :return: the alarm events in chronological order
    """
    builder = AlarmEventBuilder(rules, tag_mappings)
    return builder.build(tagged_messages)
