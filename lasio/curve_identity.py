"""Cross-trip identity resolution for well-log curves.

The same physical sensor can appear in several logging runs (trips downhole),
and a single trip may carry two probes that expose the same mnemonic
(e.g. two ``GR`` curves).  This module builds *cross-trip identities* for
curve observations using four pieces of evidence:

1. mnemonic (助记名)
2. unit (单位) -- convertible units are a necessary condition only; sharing a
   unit family never proves that two curves come from the same sensor
3. toolstring position (工具串位置)
4. confirmed serial-number mappings (已确认映射 / 探头序号)

Two hard rules always apply:

* curves logged in the same trip can never be collapsed into one identity, so
  the two GR probes of trip 2 stay distinct no matter how similar they look;
* a shared serial number is positive proof of the same sensor, while
  different serial numbers are positive proof of different sensors.

When more than one global pairing satisfies every rule, the curves whose
placement differs between the valid pairings are returned as *pending
verification* rather than being guessed.
"""

from dataclasses import dataclass, field
import os
import re

# ---------------------------------------------------------------------------
# Unit families
# ---------------------------------------------------------------------------
# Members of a family can be converted into one another (sometimes only with a
# tool-specific calibration, e.g. CPS -> gAPI).  Conversion makes the units
# *compatible*, it never makes the sensors identical.
UNIT_FAMILIES = (
    ("gamma", ("GAPI", "API", "CPS", "KCPS")),
    ("voltage", ("MV", "V")),
    ("length", ("M", "FT", "F", "METER", "METERS", "FEET")),
    ("dimensionless", ("V/V", "PU", "DEC")),
)

_UNIT_LOOKUP = {}
for _family, _units in UNIT_FAMILIES:
    for _unit in _units:
        _UNIT_LOOKUP[_unit] = _family


def unit_family(unit):
    """Return the physical-quantity family of a unit, or ``None`` if unknown.

    An empty/blank unit means "not recorded" and also returns ``None``.
    """
    if unit is None:
        return None
    return _UNIT_LOOKUP.get(str(unit).strip().upper())


def units_compatible(unit_a, unit_b):
    """True if two units are convertible.

    A blank unit is treated as "unknown" and never blocks a match, but a known
    mismatch (e.g. gamma unit vs voltage) does block it.  Convertibility is a
    necessary-not-sufficient condition for sharing an identity.
    """
    family_a = unit_family(unit_a)
    family_b = unit_family(unit_b)
    if family_a is None or family_b is None:
        return True
    return family_a == family_b


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CurveObservation:
    """One curve as recorded in one trip."""

    trip_id: str
    curve_key: str
    mnemonic: str
    unit: str
    position: str = ""
    serial: str = ""
    description: str = ""

    @property
    def label(self):
        return "%s/%s" % (self.trip_id, self.curve_key)


_GENERIC_POSITIONS = {"", "GR", "NA", "N/A", "NONE", "UNKNOWN"}
_POS_TAG = re.compile(r"POS\s*=\s*([^;]+)", re.IGNORECASE)
_SN_TAG = re.compile(r"S(N|ERIAL)\s*=\s*([^;]+)", re.IGNORECASE)


def _normalize_position(position):
    position = (position or "").strip().upper()
    return "" if position in _GENERIC_POSITIONS else position


def positions_compatible(position_a, position_b):
    """True if two toolstring positions can belong to the same sensor.

    A generic/blank position matches anything; two specific positions must be
    equal (an UPPER probe can never be reassigned as a LOWER probe).
    """
    pos_a = _normalize_position(position_a)
    pos_b = _normalize_position(position_b)
    if not pos_a or not pos_b:
        return True
    return pos_a == pos_b


def parse_description(descr):
    """Extract ``POS=...`` and ``SN=...`` tags from a LAS curve description."""
    position = ""
    serial = ""
    if descr:
        pos_match = _POS_TAG.search(descr)
        if pos_match:
            position = pos_match.group(1).strip()
        sn_match = _SN_TAG.search(descr)
        if sn_match:
            serial = sn_match.group(2).strip()
    return position, serial


def observation_from_curve_item(trip_id, curve_index, curve_item):
    """Build a :class:`CurveObservation` from a lasio ``CurveItem``.

    The depth/index curve is skipped by the caller.  Duplicate LAS mnemonics
    keep their original spelling in ``original_mnemonic`` so that the two GR
    probes of one trip are both recognised as ``GR`` while staying separate
    observations.
    """
    position, serial = parse_description(curve_item.descr)
    mnemonic = (curve_item.original_mnemonic or curve_item.mnemonic).strip()
    return CurveObservation(
        trip_id=trip_id,
        curve_key="%s[%d]" % (mnemonic or "UNKNOWN", curve_index),
        mnemonic=mnemonic.upper(),
        unit=(curve_item.unit or "").strip(),
        position=position,
        serial=serial,
        description=(curve_item.descr or "").strip(),
    )


def observations_from_las(trip_id, las):
    """Return curve observations for every non-index curve of a LAS file."""
    observations = []
    for curve_index, curve_item in enumerate(las.curves):
        if curve_index == 0:
            continue
        observations.append(
            observation_from_curve_item(trip_id, curve_index, curve_item)
        )
    return observations


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------
@dataclass
class Identity:
    """A cross-trip identity: one physical sensor and its observations."""

    identity_id: str
    serial: str
    members: list = field(default_factory=list)
    confirmed: bool = False

    @property
    def trips(self):
        return sorted({member.trip_id for member in self.members})


def _observations_compatible(ob_a, ob_b):
    if ob_a.trip_id == ob_b.trip_id:
        return False
    if ob_a.mnemonic != ob_b.mnemonic:
        return False
    if not units_compatible(ob_a.unit, ob_b.unit):
        return False
    if not positions_compatible(ob_a.position, ob_b.position):
        return False
    return True


def _cluster_compatible(observation, members):
    return all(
        _observations_compatible(observation, member) for member in members
    )


def _validate_seed_clusters(seed_clusters):
    for serial, members in seed_clusters.items():
        trips = [member.trip_id for member in members]
        if len(trips) != len(set(trips)):
            raise ValueError(
                "Confirmed serial %s is assigned to two curves in the same "
                "trip: %s" % (serial, members)
            )
        for i, member_a in enumerate(members):
            for member_b in members[i + 1 :]:
                if not _observations_compatible(member_a, member_b):
                    raise ValueError(
                        "Confirmed serial %s joins incompatible curves %s and "
                        "%s" % (serial, member_a.label, member_b.label)
                    )


def resolve_identities(observations, max_solutions=200):
    """Resolve curve observations into cross-trip identities.

    Args:
        observations (sequence of CurveObservation): every curve of every
            trip, including the un-serialized probes.
        max_solutions (int): enumeration cap; once two distinct global
            pairings are found the answer is already ambiguous.

    Returns:
        tuple: ``(identities, pending_labels, solution_count)`` where
        ``identities`` is taken from the first valid pairing and
        ``pending_labels`` lists curves whose cluster differs between valid
        pairings.  ``solution_count == 1`` means every identity is certain.
    """
    observations = list(observations)
    seen_keys = set()
    for observation in observations:
        key = (observation.trip_id, observation.curve_key)
        if key in seen_keys:
            raise ValueError("Duplicate observation %s" % (observation.label,))
        seen_keys.add(key)

    # Phase 1: seed clusters from confirmed serial numbers.
    seed_clusters = {}
    for observation in observations:
        if observation.serial:
            seed_clusters.setdefault(observation.serial, []).append(observation)
    _validate_seed_clusters(seed_clusters)

    clusters = [list(members) for members in seed_clusters.values()]
    unseeded = [ob for ob in observations if not ob.serial]

    solutions = set()

    def snapshot():
        return tuple(
            sorted(frozenset(member.label for member in cluster) for cluster in clusters)
        )

    def search(index):
        if len(solutions) >= max_solutions:
            return
        if index == len(unseeded):
            solutions.add(snapshot())
            return
        observation = unseeded[index]
        # Maximal linking: attach to every compatible existing identity in
        # turn.  Opening a brand-new identity is allowed only when no
        # compatible identity exists (e.g. a curve seen in a single trip),
        # otherwise every un-serialized curve could trivially stand alone and
        # the pairing would always be ambiguous.
        attached = False
        for cluster in clusters:
            if _cluster_compatible(observation, cluster):
                attached = True
                cluster.append(observation)
                search(index + 1)
                cluster.pop()
        if not attached:
            clusters.append([observation])
            search(index + 1)
            clusters.pop()

    search(0)

    if not solutions:
        raise ValueError("No valid global pairing exists for the observations")

    ordered_solutions = sorted(solutions, key=lambda sol: tuple(map(sorted, sol)))
    first = ordered_solutions[0]

    identities = []
    for cluster_index, member_labels in enumerate(first):
        members = [
            ob
            for label in member_labels
            for ob in observations
            if ob.label == label
        ]
        serial = next((ob.serial for ob in members if ob.serial), "")
        identities.append(
            Identity(
                identity_id="ID-%02d" % (cluster_index + 1),
                serial=serial,
                members=sorted(members, key=lambda ob: (ob.trip_id, ob.curve_key)),
                confirmed=bool(serial),
            )
        )

    # A curve is pending when it carries no confirmed serial and its cluster
    # is not identical in every valid global pairing.  Serial-seeded curves
    # always keep their seed identity, so they are never reported as pending
    # even when extra un-serialized curves may or may not join them.
    serial_labels = {
        observation.label for observation in observations if observation.serial
    }
    memberships = {}
    for solution in ordered_solutions:
        for cluster in solution:
            for label in cluster:
                memberships.setdefault(label, set()).add(cluster)
    pending_labels = sorted(
        label
        for label, variants in memberships.items()
        if len(variants) > 1 and label not in serial_labels
    )

    return identities, pending_labels, len(solutions)


def resolve_las_files(trip_paths):
    """Read one LAS file per trip and resolve their curve identities.

    Args:
        trip_paths (mapping or sequence): either ``{trip_id: path}`` or a
            sequence of file paths (the trip id is then taken from the file
            basename without extension).

    Returns:
        tuple: ``(identities, pending_labels, solution_count)``
    """
    import lasio

    observations = []
    if hasattr(trip_paths, "items"):
        items = list(trip_paths.items())
    else:
        items = [
            (os.path.splitext(os.path.basename(path))[0], path)
            for path in trip_paths
        ]
    for trip_id, path in items:
        las = lasio.read(path)
        observations.extend(observations_from_las(trip_id, las))
    return resolve_identities(observations)


def format_report(identities, pending_labels, solution_count):
    """Render a human-readable identity report (Chinese labels)."""
    lines = []
    lines.append("整体配对方案数: %d" % solution_count)
    lines.append("")
    lines.append("跨趟身份:")
    for identity in identities:
        serial = identity.serial or "无序号"
        status = "已确认" if identity.confirmed else "推导"
        member_text = ", ".join(
            "%s(%s/%s%s)%s"
            % (
                member.label,
                member.mnemonic,
                member.unit or "-",
                ("/" + member.position) if _normalize_position(member.position) else "",
                " [待核]" if member.label in pending_labels else "",
            )
            for member in identity.members
        )
        lines.append(
            "  %s [%s, 序号=%s, 趟次=%s]: %s"
            % (
                identity.identity_id,
                status,
                serial,
                "/".join(identity.trips),
                member_text,
            )
        )
    lines.append("")
    if pending_labels:
        lines.append("待核曲线 (%d 条, 存在两种及以上整体配对):" % len(pending_labels))
        for label in pending_labels:
            lines.append("  - %s" % label)
    else:
        lines.append("待核曲线: 无 (配对唯一)")
    return "\n".join(lines)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve cross-trip curve identities from one LAS per trip."
    )
    parser.add_argument(
        "las_files",
        nargs="+",
        help="LAS files in trip order; trip id defaults to each file's basename",
    )
    parser.add_argument(
        "--trip-id",
        action="append",
        default=[],
        help="override trip id for the corresponding file (repeat per file)",
    )
    args = parser.parse_args(argv)

    if args.trip_id:
        if len(args.trip_id) != len(args.las_files):
            parser.error("--trip-id must be given once per LAS file")
        trip_paths = dict(zip(args.trip_id, args.las_files))
    else:
        trip_paths = args.las_files

    identities, pending_labels, solution_count = resolve_las_files(trip_paths)
    print(format_report(identities, pending_labels, solution_count))
    return 1 if pending_labels else 0


if __name__ == "__main__":
    raise SystemExit(main())
