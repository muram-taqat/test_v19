from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

# The §2.3 truth table, written ONCE so the rule has a single spelling in the suite.
#
# The rule: a partner has coordinates only when BOTH axes are non-zero
# (`bool(partner_latitude) and bool(partner_longitude)`). The two half-zero rows are
# the only rows that distinguish this rule from the looser "both axes zero" rule three
# sibling modules use - they are what PINS the choice. Deleting either half-zero row
# silently relaxes the predicate to the sibling rule and this file stops noticing.
#
# Rationale for strictness: for a ROUTING ENDPOINT a single missing axis yields a
# position that is silently WRONG rather than absent, which is the exact defect class
# this work exists to remove. Accepted caveat: longitude 0 crosses the UK, France,
# Spain and Ghana, and the equator crosses Kenya and Indonesia - but at the field's
# declared `digits=(10, 7)` (roughly 1 cm) a real place reads `0.0012345`, never
# exactly `0.0000000`. An exact zero on one axis is a data artifact, not a place.
COORDINATE_PRESENCE_TRUTH_TABLE = [
    # (latitude, longitude, has_coordinates, why)
    (0.0, 0.0, False, "Null Island - the marker core itself writes to mean 'not geolocated'"),
    (0.0, 106.7, False, "half-filled data: the latitude axis is missing"),
    (20.86, 0.0, False, "half-filled data: the longitude axis is missing"),
    (20.86, 106.68, True, "Hai Phong - both axes actually measured"),
]


@tagged('post_install', '-at_install')
class TestGeoCoordinatePresence(TransactionCase):
    """Protects the coordinate-presence contract on `res.partner` and the
    destination-validation contract of the two distance helpers.

    ## The trap every future editor of this file must know

    `partner_latitude` and `partner_longitude` are `fields.Float`. A NULL column
    surfaces in Python as `0.0`, NEVER as `False`:

      - `Float.convert_to_record` is literally `return value or 0.0` (odoo/fields.py)
      - `Float.convert_to_column` is `float(value or 0.0)` - writing `False` stores `0.0`

    Three consequences that have already produced one dead branch and one worthless
    test in this codebase:

      1. `partner.partner_latitude is False` can never be True. The branch at
         `models/res_partner.py` that tests it has never fired.
      2. `assertFalse(partner.partner_latitude)` PASSES for a coordinate-less partner
         AND for a partner at a genuine (0,0). Any test whose red mechanism is "the
         value is falsy" is green before AND after any fix here and proves nothing.
      3. Therefore an "absent coordinate" CANNOT be constructed as a falsy value to
         drive the range check. Where these tests need a coordinate the range check
         must reject, they use an OUT-OF-RANGE value (latitude 200), never a missing
         or zero one. `test_route_section_cache.py` already established that
         convention for the same reason.

    **Do not "fix" a red in this file by asserting falsiness.** That converts a real
    failure into a test that cannot fail.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked for.

        No address field is ever passed. That is load-bearing: `to_geo_routes` depends
        on `base_geolocalize`, whose `write()` resets BOTH coordinates to (0,0) whenever
        `street`, `zip`, `city`, `state_id` or `country_id` is written without both
        coordinates in the same vals. Adding an address field to this helper would let
        core silently overwrite the very values under test.
        """
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    # ------------------------------------------------------------------
    # T1 - the presence predicate
    # ------------------------------------------------------------------
    def test_partner_counts_as_geolocated_only_when_both_axes_are_non_zero(self):
        """A partner is usable as a routing endpoint only when BOTH axes are non-zero.

        RED mechanism: `res.partner._has_geo_coordinates` does not exist on the
        unmodified tree (verified: zero hits repo-wide), so the call raises
        AttributeError. It is red because the rule has no single home yet - today it is
        spelled five different ways across five modules, in two MUTUALLY INCOMPATIBLE
        variants for the half-zero case.
        """
        for latitude, longitude, expected, why in COORDINATE_PRESENCE_TRUTH_TABLE:
            with self.subTest(latitude=latitude, longitude=longitude, why=why):
                partner = self._make_partner(
                    'Presence %s/%s' % (latitude, longitude),
                    latitude=latitude,
                    longitude=longitude,
                )
                self.assertIs(
                    partner._has_geo_coordinates(), expected,
                    "partner at (%s, %s) must%s count as geolocated - %s" % (
                        latitude, longitude, '' if expected else ' NOT', why,
                    ),
                )

    # ------------------------------------------------------------------
    # T2 - the range check, and the split it must survive
    # ------------------------------------------------------------------
    def test_range_check_rejects_a_latitude_beyond_the_poles(self):
        """Latitude 200 is not a place on Earth and must be refused.

        This pins the branch the presence/range method split must NOT disturb, so the
        22 call sites across 7 modules that only ever cared about range - notably the
        already-shipped `18.0.1.0.3` migration, which calls with
        `raise_if_not_valid=False` and leaves a row untouched when the answer is False -
        keep their behaviour bit-for-bit.

        Both call shapes are asserted because both are in production use today, and
        each is a different contract:
          - bare call        -> raises ValidationError (what every provider adapter relies on)
          - raise_if_not_valid=False -> returns falsy   (what the shipped migration relies on)

        RED mechanism: `_check_coordinate_range` does not exist on the unmodified tree
        -> AttributeError.
        """
        out_of_range = self._make_partner('Beyond the north pole', latitude=200.0, longitude=106.7)

        with self.assertRaises(ValidationError, msg="latitude 200 must be refused by the range check"):
            out_of_range._check_coordinate_range()

        self.assertFalse(
            out_of_range._check_coordinate_range(raise_if_not_valid=False),
            "with raise_if_not_valid=False the range check must REPORT the refusal instead of "
            "raising - the shipped 18.0.1.0.3 migration depends on this exact shape to leave a "
            "row as found rather than destroying its distance",
        )

    def test_range_check_stays_silent_about_presence(self):
        """The range check answers RANGE only - never presence.

        A half-zero coordinate (0.0, 106.7) is a perfectly in-range pair of numbers and
        the range check must accept it, even though the presence predicate rejects it
        (see the truth table). This is what makes the split a real separation of
        concerns rather than a rename: if the range method ever starts refusing
        half-zero pairs, every range-only caller silently changes behaviour and the
        whole point of the split is lost.

        RED mechanism: `_check_coordinate_range` does not exist -> AttributeError.
        """
        half_zero = self._make_partner('Half-filled but in range', latitude=0.0, longitude=106.7)

        self.assertTrue(
            half_zero._check_coordinate_range(),
            "(0.0, 106.7) is within [-90, 90] x [-180, 180], so the RANGE check must accept it - "
            "rejecting it here would leak presence semantics into the range branch and change "
            "behaviour for every range-only caller",
        )

    # ------------------------------------------------------------------
    # T3 - the destination-validation defect
    # ------------------------------------------------------------------
    def test_rhumline_distance_rejects_an_out_of_range_destination(self):
        """A rhumline distance must not be computed to an impossible destination.

        THE DEFECT: `rhumline_distance` validates `(partner1 + partner1)` - the ORIGIN
        twice - so the destination is never inspected. `haversine_distance` correctly
        validates `(partner1 + partner2)`.

        RED mechanism (and this is the one test here that goes red on a genuine
        ASSERTION FAILURE rather than an AttributeError): today the origin is valid, so
        `_validate_coordinates` passes; `radians(200.0)` then computes happily and a
        float is returned. Nothing raises, so `assertRaises` fails with
        "ValidationError not raised".

        The destination is OUT OF RANGE, not missing, and that is deliberate: a
        "missing" coordinate reads `0.0` (see the class docstring), which is in range
        and sails through the check today AND after the fix - a zero-based variant of
        this test would be green today and would prove nothing.
        """
        origin = self._make_partner('Hai Phong origin', latitude=20.861135, longitude=106.672549)
        impossible_destination = self._make_partner('Beyond the north pole', latitude=200.0, longitude=106.7)

        with self.assertRaises(ValidationError):
            self.env['res.partner'].rhumline_distance(origin, impossible_destination)

    def test_haversine_distance_rejects_an_out_of_range_destination_control_already_correct(self):
        """CONTROL - already GREEN today. Must STAY green.

        This is the sibling that already validates `(partner1 + partner2)` correctly.
        It is the reference behaviour the rhumline test above demands, and it is here so
        that a regression which breaks BOTH helpers is distinguishable from the current
        state where only rhumline is broken.
        """
        origin = self._make_partner('Hai Phong origin', latitude=20.861135, longitude=106.672549)
        impossible_destination = self._make_partner('Beyond the north pole', latitude=200.0, longitude=106.7)

        with self.assertRaises(ValidationError):
            self.env['res.partner'].haversine_distance(origin, impossible_destination)

    # ------------------------------------------------------------------
    # T4 - characterization / pin (NOT red-before-green evidence)
    # ------------------------------------------------------------------
    def test_absent_coordinate_is_sql_null_yet_reads_as_zero_in_python_characterization_pin(self):
        """CHARACTERIZATION / PIN - GREEN before this change and GREEN after, by design.

        This test asserts CORE ORM behaviour that this work deliberately does NOT alter.
        It is not evidence that anything was fixed. Its value is that the entire
        coordinate-presence design rests on the NULL/0.0 boundary behaving exactly this
        way, and if a future Odoo release moves that boundary this test fails loudly and
        names the assumption instead of letting the design rot silently.

        Three observation points, which do not agree with each other:
          1. The DB genuinely stores NULL for a coordinate that was never written.
          2. Python cannot see it - the attribute reads `0.0`, never `False`. This is
             why `partner_latitude is False` is dead code.
          3. Aggregation CAN see it - `COUNT(col)` ignores NULLs - which is the one
             honest exception to "the ORM cannot distinguish them".

        Point 3 changes no decision: the distinction is destroyed by the first ORM
        write, so it is not a foundation anything can be built on. It is asserted
        because the design states it, and an unasserted claim decays.
        """
        absent = self._make_partner('Never given coordinates')
        explicit_zero = self._make_partner('Explicitly at Null Island', latitude=0.0, longitude=0.0)
        self.env.flush_all()

        # 1. The DB distinguishes them.
        self.env.cr.execute(
            "SELECT partner_latitude IS NULL, partner_longitude IS NULL FROM res_partner WHERE id IN %s ORDER BY id",
            ((absent.id, explicit_zero.id),),
        )
        (absent_lat_null, absent_lon_null), (zero_lat_null, zero_lon_null) = self.env.cr.fetchall()
        self.assertTrue(absent_lat_null, "a coordinate never written must be SQL NULL, not 0.0")
        self.assertTrue(absent_lon_null, "a coordinate never written must be SQL NULL, not 0.0")
        self.assertFalse(zero_lat_null, "an explicitly written 0.0 must be stored as 0.0, not NULL")
        self.assertFalse(zero_lon_null, "an explicitly written 0.0 must be stored as 0.0, not NULL")

        # 2. Python cannot. Both rows read an identical 0.0, and neither reads False.
        self.assertEqual(
            absent.partner_latitude, 0.0,
            "Float.convert_to_record is `return value or 0.0`, so a NULL column must read 0.0",
        )
        self.assertIsNot(
            absent.partner_latitude, False,
            "the ORM must never hand back the False singleton for a Float - this is precisely why "
            "`partner_latitude is False` is dead code that has never fired",
        )
        self.assertEqual(
            absent.partner_latitude, explicit_zero.partner_latitude,
            "a NULL coordinate and a stored 0.0 must be INDISTINGUISHABLE on attribute read - if "
            "this ever fails, the presence predicate's whole justification changes",
        )

        # 3. Aggregation can - COUNT(col) ignores NULLs.
        [(absent_count,)] = self.env['res.partner']._read_group(
            [('id', '=', absent.id)], [], ['partner_latitude:count'],
        )
        [(zero_count,)] = self.env['res.partner']._read_group(
            [('id', '=', explicit_zero.id)], [], ['partner_latitude:count'],
        )
        self.assertEqual(absent_count, 0, "COUNT ignores NULLs, so a never-written coordinate must count 0")
        self.assertEqual(zero_count, 1, "a stored 0.0 is a value, so COUNT must see it")
