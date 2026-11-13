"""
Geographic (true-north) wind observations from MWV wind sentences.

An MWV sentence carries wind against one of two references:

  R - relative (apparent) wind, measured against the vessel's bow
  T - true wind, already referenced to geographic north by the instrument

Relative wind only becomes a geographic wind vector once the vessel's speed
and bow heading are known.  Those arrive on separate sentences that update at
their own (different) rates, so the processor keeps the most recent sample of
each stream and records the exact source sentence of every number used.

Two angles must never be conflated:

  heading      - where the bow points (HDT, THS, OSD heading, VHW true heading)
  course/track - where the vessel travels over ground (RMC course, VTG track,
                 OSD course)

With leeway (cross drift) they differ.  The heading rotates the apparent wind
out of the ship frame; the ground velocity vector - speed over ground in the
course direction - removes the vessel's motion.  Track made good is therefore
never accepted as a heading, and a scalar speed with no course is only applied
along the heading with an explicit no-leeway assumption in the notes.
Magnetic headings (HDG/HDM) are not used because they need an external
magnetic variation to become true headings.
"""

import math


# MWV wind speed units -> knots
_WIND_SPEED_TO_KNOTS = {
    'N': 1.0,                  # knots
    'K': 0.5399568034557235,   # kilometres per hour
    'M': 1.9438444924406046,   # metres per second
}

# OSD adds statute miles per hour on top of the MWV unit letters
_OSD_SPEED_TO_KNOTS = dict(_WIND_SPEED_TO_KNOTS)
_OSD_SPEED_TO_KNOTS['S'] = 0.868976241900648

_KMH_PER_KNOT = 1.852

_HEADING_SENTENCES = ('HDT', 'THS', 'OSD', 'VHW')
_SPEED_SENTENCES = ('RMC', 'VTG', 'VBW', 'VHW', 'OSD')


def _as_float(value):
    if value is None or value == '':
        return None
    return float(value)


def _to_knots(speed, units, unit_table):
    speed = _as_float(speed)
    if speed is None:
        return None
    factor = unit_table.get((units or '').upper())
    if factor is None:
        raise ValueError('unsupported speed unit: %r' % units)
    return speed * factor


def _source_id(sentence):
    return '%s%s' % (sentence.talker, sentence.sentence_type)


def true_wind_from_relative(relative_angle, relative_speed_knots,
                            heading_true, vessel_speed_knots,
                            vessel_course_true):
    """
    Convert relative (apparent) wind to true wind in geographic coordinates.

    The MWV angle is the direction the wind comes FROM, measured clockwise
    from the bow.  ``heading_true`` orients the apparent wind to true north;
    ``vessel_speed_knots``/``vessel_course_true`` give the vessel velocity
    vector (course equals heading only when there is no leeway).

    Returns a ``(wind_from_direction_true, wind_speed_knots)`` tuple.
    """
    apparent_from = (heading_true + relative_angle) % 360.0

    # Apparent wind velocity (toward-vector) in the geographic frame.
    apparent_east = -relative_speed_knots * math.sin(math.radians(apparent_from))
    apparent_north = -relative_speed_knots * math.cos(math.radians(apparent_from))

    # Vessel ground/water velocity vector, along the course made good.
    vessel_east = vessel_speed_knots * math.sin(math.radians(vessel_course_true))
    vessel_north = vessel_speed_knots * math.cos(math.radians(vessel_course_true))

    true_east = apparent_east + vessel_east
    true_north = apparent_north + vessel_north

    wind_from = math.degrees(math.atan2(-true_east, -true_north)) % 360.0
    return wind_from, math.hypot(true_east, true_north)


class StreamSample(object):
    """A scalar reading paired with the sentence it was taken from."""

    def __init__(self, value, sentence):
        self.value = value
        self.sentence = sentence
        self.source = _source_id(sentence)
        self.timestamp = getattr(sentence, 'timestamp', None)

    def __repr__(self):
        return '<StreamSample(value=%r, source=%r)>' % (self.value, self.source)


class VelocitySample(object):
    """
    A vessel velocity reading paired with its source sentence.

    ``course_true`` is ``None`` for scalar speeds or component vectors that
    still need a heading to be rotated into the geographic frame.
    """

    def __init__(self, sentence, speed_knots, reference, course_true=None,
                 components=None):
        self.sentence = sentence
        self.source = _source_id(sentence)
        self.timestamp = getattr(sentence, 'timestamp', None)
        self.speed_knots = speed_knots
        self.reference = reference
        self.course_true = course_true
        # (longitudinal/bow, transverse/starboard) components in knots,
        # as reported by VBW.
        self.components = components

    def resolve(self, heading_true):
        """Return ``(speed_knots, course_true)`` in the geographic frame."""
        if self.components is not None:
            longitudinal, transverse = self.components
            heading = math.radians(heading_true)
            east = (longitudinal * math.sin(heading)
                    + transverse * math.cos(heading))
            north = (longitudinal * math.cos(heading)
                     - transverse * math.sin(heading))
            return math.hypot(east, north), math.degrees(
                math.atan2(east, north)) % 360.0
        if self.course_true is not None:
            return self.speed_knots, self.course_true
        # Scalar speed with no direction: assume it points along the bow.
        return self.speed_knots, heading_true

    def __repr__(self):
        return ('<VelocitySample(source=%r, speed=%r, course=%r, '
                'reference=%r)>' % (
                    self.source, self.speed_knots, self.course_true,
                    self.reference))


class WindObservation(object):
    """
    A wind observation expressed against geographic (true) north.

    Wind direction is always the meteorological convention: the bearing the
    wind blows FROM.  Quantities that cannot be determined stay ``None`` and
    ``notes``/``missing`` explain why.
    """

    def __init__(self, mwv):
        self.mwv_source = _source_id(mwv)
        self.status = mwv.status
        self.reference = mwv.reference
        self.apparent = mwv.reference == 'R'

        self.relative_wind_angle = _as_float(mwv.wind_angle)
        self.relative_wind_speed_knots = _to_knots(
            mwv.wind_speed, mwv.wind_speed_units, _WIND_SPEED_TO_KNOTS)

        # True (geographic) wind, populated when it can be established.
        self.wind_direction = None
        self.wind_speed_knots = None

        # Apparent wind rotated into geographic bearings (heading known but
        # the vessel velocity cannot be removed yet).
        self.apparent_wind_direction_true = None

        # Vessel motion actually applied to the conversion.
        self.heading = None
        self.heading_source = None
        self.speed_knots = None
        self.speed_source = None
        self.speed_reference = None
        self.course_true = None
        self.course_source = None

        self.available = ['relative_wind_angle', 'relative_wind_speed_knots']
        self.missing = []
        self.notes = []

    def __repr__(self):
        return (
            '<WindObservation(source=%r, apparent=%r, direction=%r, '
            'speed_knots=%r)>' % (
                self.mwv_source, self.apparent,
                self.wind_direction, self.wind_speed_knots))


class WindProcessor(object):
    """
    Stateful combiner for MWV, heading and speed sentences.

    Feed every parsed sentence to :meth:`update`.  Heading and speed streams
    are remembered at their own update rates; each MWV is resolved against
    the most recent samples, and the source sentence of each sample is kept
    on the resulting :class:`WindObservation`.
    """

    def __init__(self):
        self.heading = None
        self.ground_velocity = None
        self.water_velocity = None
        self.osd_velocity = None
        self.observations = []

    def update(self, sentence):
        """
        Feed one parsed NMEA sentence.

        Returns a :class:`WindObservation` for MWV input, otherwise ``None``
        (heading/speed sentences only refresh the internal state).
        """
        if sentence.sentence_type == 'MWV':
            observation = self.process(sentence)
            self.observations.append(observation)
            return observation
        self._update_heading(sentence)
        self._update_speed(sentence)
        return None

    def process(self, mwv):
        """Resolve a single MWV sentence against the current state."""
        if mwv.reference not in ('R', 'T'):
            raise ValueError(
                'MWV reference must be R (relative) or T (true), got %r'
                % mwv.reference)

        observation = WindObservation(mwv)

        if (observation.relative_wind_angle is None
                or observation.relative_wind_speed_knots is None):
            raise ValueError(
                'MWV is missing the wind angle or speed needed to build a '
                'wind observation')

        if mwv.status != 'A':
            observation.notes.append(
                'MWV status is %r, not the valid flag A' % mwv.status)

        if mwv.reference == 'T':
            # The instrument already reports geographic true wind: the vessel
            # motion must not be applied a second time.
            observation.wind_direction = observation.relative_wind_angle
            observation.wind_speed_knots = observation.relative_wind_speed_knots
            observation.available.extend(
                ['true_wind_direction', 'true_wind_speed_knots'])
            observation.notes.append(
                'native true-wind MWV passed through without vessel '
                'motion correction')
            return observation

        self._resolve_relative(observation)
        return observation

    def _resolve_relative(self, observation):
        heading_sample = self.heading
        velocity = self._select_velocity()

        if heading_sample is not None:
            observation.heading = heading_sample.value
            observation.heading_source = heading_sample.source
            observation.apparent_wind_direction_true = (
                heading_sample.value + observation.relative_wind_angle) % 360.0
            observation.available.append('apparent_wind_direction_true')
        else:
            observation.missing.append(
                'bow heading (one of %s)' % ', '.join(_HEADING_SENTENCES))
            observation.notes.append(
                'no bow heading: relative wind stays in the ship frame and '
                'no geographic bearing can be produced; track made good is '
                'not a heading substitute when the vessel makes leeway')

        if velocity is not None:
            observation.speed_knots = velocity.speed_knots
            observation.speed_source = velocity.source
            observation.speed_reference = velocity.reference
        else:
            observation.missing.append(
                'vessel speed (one of %s)' % ', '.join(_SPEED_SENTENCES))

        if heading_sample is None or velocity is None:
            self._annotate_incomplete(observation, heading_sample, velocity)
            return

        speed_knots, course_true = velocity.resolve(heading_sample.value)
        observation.speed_knots = speed_knots
        observation.course_true = course_true
        if velocity.course_true is not None:
            observation.course_source = velocity.source
        elif velocity.components is not None:
            observation.course_source = velocity.source
            observation.notes.append(
                'course derived from %s longitudinal/transverse components '
                'rotated by the heading' % velocity.source)
        else:
            observation.course_source = heading_sample.source
            observation.notes.append(
                'scalar speed with no course: applied along the bow heading, '
                'which assumes no leeway')

        if velocity.reference == 'water':
            observation.notes.append(
                'only water-referenced speed available; the conversion '
                'ignores current and yields wind relative to the water')
        elif velocity.reference == 'osd':
            observation.notes.append(
                'OSD speed used (reference field %r); confirm whether it is '
                'ground- or water-referenced'
                % velocity.sentence.speed_ref)

        wind_from, wind_speed = true_wind_from_relative(
            observation.relative_wind_angle,
            observation.relative_wind_speed_knots,
            observation.heading,
            speed_knots,
            course_true)
        observation.wind_direction = wind_from
        observation.wind_speed_knots = wind_speed
        observation.available.extend(
            ['true_wind_direction', 'true_wind_speed_knots'])

    @staticmethod
    def _annotate_incomplete(observation, heading_sample, velocity):
        if heading_sample is not None and velocity is None:
            observation.notes.append(
                'heading available but vessel speed missing: the apparent '
                'wind bearing relative to true north and the apparent wind '
                'speed are known, but true wind cannot be derived because '
                'the vessel velocity vector is unknown')
        elif velocity is not None and heading_sample is None:
            observation.notes.append(
                'vessel speed available but bow heading missing: speed '
                'cannot rotate the relative wind into geographic '
                'coordinates, so only the reported relative angle and speed '
                'are available; a ground track/course is not a heading')
        else:
            observation.notes.append(
                'neither heading nor vessel speed available: only the '
                'relative angle and speed reported by the MWV are known')

    def _select_velocity(self):
        # Ground-referenced vectors first (they capture leeway correctly),
        # then explicit water-referenced speeds, and finally the OSD sample
        # whose speed reference field has to be confirmed by the caller.
        if self.ground_velocity is not None:
            return self.ground_velocity
        if self.water_velocity is not None:
            return self.water_velocity
        if self.osd_velocity is not None:
            return self.osd_velocity
        return None

    def _update_heading(self, sentence):
        sentence_type = sentence.sentence_type
        heading = None

        if sentence_type == 'HDT':
            heading = _as_float(sentence.heading)
        elif sentence_type == 'THS':
            if sentence.status == 'A':
                heading = _as_float(sentence.heading)
        elif sentence_type == 'OSD':
            if sentence.status == 'A':
                heading = _as_float(sentence.heading)
        elif sentence_type == 'VHW':
            heading = _as_float(sentence.heading_true)

        if heading is not None:
            self.heading = StreamSample(heading, sentence)

    def _update_speed(self, sentence):
        sentence_type = sentence.sentence_type

        if sentence_type == 'RMC':
            if sentence.is_valid:
                speed = _as_float(sentence.spd_over_grnd)
                if speed is not None:
                    self.ground_velocity = VelocitySample(
                        sentence, speed, 'ground',
                        course_true=_as_float(sentence.true_course))

        elif sentence_type == 'VTG':
            speed = _as_float(sentence.spd_over_grnd_kts)
            if speed is None:
                kmh = _as_float(sentence.spd_over_grnd_kmph)
                if kmh is not None:
                    speed = kmh / _KMH_PER_KNOT
            if speed is not None:
                self.ground_velocity = VelocitySample(
                    sentence, speed, 'ground',
                    course_true=_as_float(sentence.true_track))

        elif sentence_type == 'VBW':
            # Longitudinal (bow) and transverse (starboard) ground speeds;
            # the transverse component is what carries leeway drift and must
            # not be dropped.
            if sentence.data_validity_grnd_spd == 'A':
                longitudinal = _as_float(sentence.lon_grnd_spd)
                if longitudinal is not None:
                    transverse = _as_float(sentence.trans_grnd_spd) or 0.0
                    self.ground_velocity = VelocitySample(
                        sentence,
                        math.hypot(longitudinal, transverse),
                        'ground',
                        components=(longitudinal, transverse))
            if sentence.data_validity_water_spd == 'A':
                longitudinal = _as_float(sentence.lon_water_spd)
                if longitudinal is not None:
                    transverse = _as_float(sentence.trans_water_spd) or 0.0
                    self.water_velocity = VelocitySample(
                        sentence,
                        math.hypot(longitudinal, transverse),
                        'water',
                        components=(longitudinal, transverse))

        elif sentence_type == 'VHW':
            speed = _as_float(sentence.water_speed_knots)
            if speed is None:
                kmh = _as_float(sentence.water_speed_km)
                if kmh is not None:
                    speed = kmh / _KMH_PER_KNOT
            if speed is not None:
                # Scalar water speed: direction unknown, assumed along bow.
                self.water_velocity = VelocitySample(
                    sentence, speed, 'water')

        elif sentence_type == 'OSD':
            if sentence.status == 'A':
                speed = _to_knots(
                    sentence.speed, sentence.speed_unit, _OSD_SPEED_TO_KNOTS)
                if speed is not None:
                    course = None
                    if sentence.course_true == 'T':
                        course = _as_float(sentence.course)
                    self.osd_velocity = VelocitySample(
                        sentence, speed, 'osd', course_true=course)


def process_sentences(sentences):
    """Convenience helper: reduce an iterable of parsed sentences."""
    processor = WindProcessor()
    for sentence in sentences:
        processor.update(sentence)
    return processor
