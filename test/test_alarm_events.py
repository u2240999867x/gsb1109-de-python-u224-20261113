'''
Tests the alarm event aggregation module.
'''

import inspect
import os
import struct
import unittest
from io import BytesIO

import pyulog
from pyulog.alarm_events import (ALREADY_IN_ALARM, AlarmEventBuilder,
                                 AlarmRule, TagMapping, build_alarm_events)

TEST_PATH = os.path.dirname(os.path.abspath(
    inspect.getfile(inspect.currentframe())))

TAG_A = 1
TAG_B = 2


def temp_rule():
    ''' Temperature alarm: overheat raises, the ok line clears it. '''
    return AlarmRule(
        alarm_id='temp',
        is_alarm=lambda message: 'OVERHEAT' in message,
        description='temperature alarm')


def vibration_rule():
    return AlarmRule(
        alarm_id='vib',
        is_alarm=lambda message: 'VIBRATION' in message)


def msg(timestamp, tag, message, log_level=ord('3')):
    ''' Minimal stand-in for a ULog MessageLoggingTagged object. '''
    return type('TaggedLogMessage', (), {
        'timestamp': timestamp,
        'tag': tag,
        'log_level': log_level,
        'message': message,
    })()


class TestAlarmEvents(unittest.TestCase):
    ''' Alarm event aggregation behaviour. '''

    def test_single_module_repetition_and_recovery(self):
        ''' Repeated errors collapse into one event with a repeat count. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        tagged = {TAG_A: [
            msg(1_000, TAG_A, 'OVERHEAT 80C'),
            msg(2_000, TAG_A, 'OVERHEAT 81C'),
            msg(3_000, TAG_A, 'OVERHEAT 82C'),
            msg(4_000, TAG_A, 'TEMP OK'),
        ]}

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.alarm_id, 'temp')
        self.assertEqual(event.tag, TAG_A)
        self.assertEqual(event.start_timestamp, 1_000)
        self.assertEqual(event.repeat_count, 2)
        self.assertEqual(len(event.sources), 3)
        self.assertTrue(event.recovered)
        self.assertEqual(event.recovery.timestamp, 4_000)
        self.assertEqual(event.end_timestamp, 4_000)
        self.assertFalse(event.already_in_alarm)

    def test_sources_trace_back_to_every_original_message(self):
        ''' Each message counted as a repetition stays findable on the event. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        originals = [
            msg(1_000, TAG_A, 'OVERHEAT 80C'),
            msg(2_000, TAG_A, 'OVERHEAT 81C'),
            msg(3_000, TAG_A, 'OVERHEAT 82C'),
        ]
        tagged = {TAG_A: originals}

        events = build_alarm_events(tagged, rules, mappings)

        event = events[0]
        self.assertEqual(len(event.sources), len(originals))
        for original, source in zip(originals, event.sources):
            # The event keeps every original message so it can be looked up.
            self.assertIs(source.original, original)
            self.assertEqual(source.message, original.message)
            self.assertEqual(source.timestamp, original.timestamp)

    def test_recovery_does_not_cross_modules(self):
        ''' A recovery from one module keeps the other module in alarm. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp'], TAG_B: ['temp']})]
        tagged = {
            TAG_A: [
                msg(1_000, TAG_A, 'OVERHEAT A'),
                msg(3_000, TAG_A, 'TEMP OK'),
            ],
            TAG_B: [
                msg(2_000, TAG_B, 'OVERHEAT B'),
                msg(4_000, TAG_B, 'OVERHEAT B again'),
                msg(5_000, TAG_B, 'TEMP OK'),
            ],
        }

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual(len(events), 2)
        by_tag = {event.tag: event for event in events}
        event_a = by_tag[TAG_A]
        event_b = by_tag[TAG_B]
        self.assertTrue(event_a.recovered)
        self.assertEqual(event_a.recovery.timestamp, 3_000)
        # Module B stays in alarm until its own recovery at 5000.
        self.assertEqual(event_b.start_timestamp, 2_000)
        self.assertEqual(event_b.repeat_count, 1)
        self.assertEqual(event_b.recovery.timestamp, 5_000)
        self.assertEqual(
            [source.timestamp for source in event_b.sources],
            [2_000, 4_000])

    def test_new_episode_after_recovery(self):
        ''' The same module can open a second event after recovering. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        tagged = {TAG_A: [
            msg(1_000, TAG_A, 'OVERHEAT 1'),
            msg(2_000, TAG_A, 'TEMP OK'),
            msg(3_000, TAG_A, 'OVERHEAT 2'),
            msg(4_000, TAG_A, 'TEMP OK'),
        ]}

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].start_timestamp, 1_000)
        self.assertEqual(events[1].start_timestamp, 3_000)
        for event in events:
            self.assertTrue(event.recovered)
            self.assertEqual(event.repeat_count, 0)

    def test_already_in_alarm_without_invented_start(self):
        ''' A pre-existing alarm has no start and flags every repeat. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        builder = AlarmEventBuilder(rules, mappings)
        builder.mark_active(TAG_A, 'temp')

        events = builder.build({TAG_A: [
            msg(2_000, TAG_A, 'OVERHEAT still'),
            msg(3_000, TAG_A, 'OVERHEAT still'),
            msg(4_000, TAG_A, 'TEMP OK'),
        ]})

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsNone(event.start_timestamp)
        self.assertTrue(event.already_in_alarm)
        self.assertEqual(event.start_note, ALREADY_IN_ALARM)
        # Both observed errors are repetitions of the unseen original.
        self.assertEqual(event.repeat_count, 2)
        self.assertEqual(len(event.sources), 2)
        self.assertTrue(event.recovered)
        self.assertEqual(event.end_timestamp, 4_000)

    def test_already_in_alarm_recovers_without_any_observed_error(self):
        ''' A recovery alone closes a pre-existing, start-less event. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        builder = AlarmEventBuilder(rules, mappings)
        builder.mark_active(TAG_A, 'temp')

        events = builder.build({TAG_A: [msg(4_000, TAG_A, 'TEMP OK')]})

        event = events[0]
        self.assertTrue(event.already_in_alarm)
        self.assertIsNone(event.start_timestamp)
        self.assertEqual(event.sources, [])
        self.assertTrue(event.recovered)

    def test_unrecovered_event_stays_open(self):
        ''' Without a recovery the event remains active at the last message. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp']})]
        tagged = {TAG_A: [
            msg(1_000, TAG_A, 'OVERHEAT 1'),
            msg(5_000, TAG_A, 'OVERHEAT 2'),
        ]}

        events = build_alarm_events(tagged, rules, mappings)

        event = events[0]
        self.assertFalse(event.recovered)
        self.assertIsNone(event.recovery)
        self.assertEqual(event.end_timestamp, 5_000)

    def test_mapping_validity_window_is_respected(self):
        ''' Messages outside every mapping's validity period are ignored. '''
        rules = [temp_rule()]
        mappings = [TagMapping(2_000, 3_000, {TAG_A: ['temp']})]
        tagged = {TAG_A: [
            msg(1_000, TAG_A, 'OVERHEAT before window'),
            msg(2_500, TAG_A, 'OVERHEAT in window'),
            msg(4_000, TAG_A, 'TEMP OK after window'),
        ]}

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.start_timestamp, 2_500)
        self.assertFalse(event.recovered)

    def test_successive_mappings_apply_by_time(self):
        ''' A later mapping can route the same tag to a different rule. '''
        rules = [temp_rule(), vibration_rule()]
        mappings = [
            TagMapping(0, 2_500, {TAG_A: ['temp']}),
            TagMapping(2_501, 10_000, {TAG_A: ['vib']}),
        ]
        tagged = {TAG_A: [
            msg(1_000, TAG_A, 'OVERHEAT 1'),
            msg(2_000, TAG_A, 'TEMP OK'),
            msg(3_000, TAG_A, 'VIBRATION high'),
        ]}

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual([event.alarm_id for event in events],
                         ['temp', 'vib'])
        self.assertTrue(events[0].recovered)
        self.assertFalse(events[1].recovered)

    def test_events_are_chronological_across_modules(self):
        ''' Events are ordered by the time of their first message. '''
        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp'], TAG_B: ['temp']})]
        tagged = {
            TAG_A: [msg(5_000, TAG_A, 'OVERHEAT A')],
            TAG_B: [msg(1_000, TAG_B, 'OVERHEAT B')],
        }

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual([event.tag for event in events], [TAG_B, TAG_A])

    def test_unknown_rule_and_tag_are_ignored(self):
        ''' Mappings referencing missing rules or tags produce no events. '''
        rules = [temp_rule()]
        mappings = [
            TagMapping(0, 10_000, {TAG_A: ['temp'], TAG_B: ['missing']}),
        ]
        tagged = {
            TAG_A: [msg(1_000, TAG_A, 'OVERHEAT A')],
            TAG_B: [msg(2_000, TAG_B, 'whatever')],
        }

        events = build_alarm_events(tagged, rules, mappings)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tag, TAG_A)

    def test_mark_active_rejects_unknown_rule(self):
        with self.assertRaises(KeyError):
            AlarmEventBuilder([temp_rule()], []).mark_active(TAG_A, 'unknown')

    def test_build_from_ulog_messages(self):
        ''' The builder consumes tagged messages parsed from a real ULog. '''
        def tagged_payload(tag, timestamp, message):
            return (bytes([ord('3')]) + struct.pack('<H', tag) +
                    struct.pack('<Q', timestamp) + message.encode('utf-8'))

        content = pyulog.ULog.HEADER_BYTES + b'\x01' + struct.pack('<Q', 0)
        for payload in [
                tagged_payload(TAG_A, 1_000, 'OVERHEAT 1'),
                tagged_payload(TAG_A, 2_000, 'OVERHEAT 2'),
                tagged_payload(TAG_B, 1_500, 'OVERHEAT B'),
                tagged_payload(TAG_A, 3_000, 'TEMP OK')]:
            content += struct.pack('<HB', len(payload), ord('C')) + payload

        ulog = pyulog.ULog(BytesIO(content))
        self.assertEqual(set(ulog.logged_messages_tagged.keys()),
                         {TAG_A, TAG_B})

        rules = [temp_rule()]
        mappings = [TagMapping(0, 10_000, {TAG_A: ['temp'], TAG_B: ['temp']})]
        events = AlarmEventBuilder(rules, mappings).build_from_ulog(ulog)

        self.assertEqual(len(events), 2)
        by_tag = {event.tag: event for event in events}
        self.assertEqual(by_tag[TAG_A].repeat_count, 1)
        self.assertTrue(by_tag[TAG_A].recovered)
        self.assertFalse(by_tag[TAG_B].recovered)
        self.assertEqual(by_tag[TAG_A].sources[0].message, 'OVERHEAT 1')
        # Sources point back to the exact parsed ULog message objects.
        self.assertIs(by_tag[TAG_A].sources[0].original,
                      ulog.logged_messages_tagged[TAG_A][0])


# vim: set et fenc=utf-8 ft=python ff=unix sts=4 sw=4 ts=4
