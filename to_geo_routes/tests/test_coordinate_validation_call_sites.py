from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCoordinateValidationCallSites(TransactionCase):
    """Covers EVERY ``_validate_coordinates`` call site this module owns, on the
    PRESENCE branch (Viindoo/tvtmaaddons#14409 close-out, work-stream B).

    ## The precedent this file sets, in one sentence

    **At every ``_validate_coordinates`` call site the raise STAYS exactly as it is,
    and the remedy is a characterization test that drives the module's own
    direction / matrix path with an UN-GEOCODED partner and asserts the
    ``ValidationError`` NAMES THAT PARTNER - never a degrade to a silent
    ``no_coordinates`` status.**

    That sentence binds the five sibling provider modules
    (``to_geo_routes_google``, ``_here``, ``_mapbox``, ``_vietmap``, ``_searoutes``)
    for the ten sites they own between them. The reason the raise stays: the operator
    pressed a button about a specific route, and a silent status tells them nothing
    and gives them nowhere to go, whereas the exception names the address they have
    to fill in.

    ## The five sites this module owns, and where each is covered

    ===================================  ====================================  ==============================
    site                                 enclosing method                      covered by
    ===================================  ====================================  ==============================
    ``res_partner.py:66``                ``haversine_distance``                T1 here (+ the range branch in
                                                                               ``test_geo_coordinates_presence``)
    ``res_partner.py:97``                ``rhumline_distance``                 T2 here (+ ditto)
    ``geo_direction.py:44``              ``_compute_haversine_matrix``         T3 here
    ``geo_direction_manual.py:60``       ``_get_directions``                   T4 here
    ``geo_direction_manual.py:171``      ``_get_distance_matrix``              T5 here
    ===================================  ====================================  ==============================

    No site is left uncovered.

    ## PRESENCE, not range - and why that is the whole point

    ``test_geo_coordinates_presence.py`` already drives sites 1 and 2 through the
    RANGE branch (latitude 200). These tests drive the PRESENCE branch instead, with
    a partner that has no coordinates at all, because that is the branch a
    ``_has_geo_coordinates`` regression silently removes: the presence test this
    module ships replaced a dead ``partner_latitude is False`` check that could never
    fire (a ``fields.Float`` reads NULL back as ``0.0``, never ``False``), so a
    partner at (0,0) used to sail through and get routed from Null Island. Every test
    here goes RED when that presence branch is neutered - which is the mutation the
    close-out gate requires, and the reason a range-only test is not a substitute.

    ## Why these are DELIVERY EVIDENCE, not non-regression guards

    Measured against the PR base (``origin/18.0``), every test in this file is RED.
    The presence check that ships at the base reads
    ``if r.partner_latitude is False or r.partner_longitude is False``, and a
    ``fields.Float`` NEVER reads back ``False`` - a NULL column reads ``0.0`` - so that
    branch is DEAD pre-PR: an un-geocoded partner clears it, clears the range branch
    too (``0.0`` is in range), and NO ``ValidationError`` is raised.
    ``_has_geo_coordinates`` does not exist at the base at all.

    These tests are green only because commit ``e79e9fc3c6`` ("[FIX] to_geo_routes: a
    coordinate of 0 was indistinguishable from never measured"), earlier in THIS SAME
    PR, delivered ``_has_geo_coordinates`` and rebuilt ``_validate_coordinates`` on top
    of it. They were authored against an intra-PR tree that already carried that fix,
    which is how they came to wear guard names they had not earned.

    The guard suffix is therefore removed: their green is evidence that THIS PR's fix
    works, and no reader should mistake it for behaviour that already shipped. They do
    also serve as regression guards from here on - but that is their second job, not
    their first.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # Hai Phong - a real, in-range, fully measured pair.
        cls.located = cls.env['res.partner'].create({
            'name': 'Call-site located partner',
            'partner_latitude': 20.861135,
            'partner_longitude': 106.672549,
        })
        cls.other_located = cls.env['res.partner'].create({
            'name': 'Call-site other located partner',
            'partner_latitude': 20.9522972,
            'partner_longitude': 107.0781387,
        })
        # No coordinate key is passed at all: both columns stay SQL NULL, which reads
        # back as `0.0` - core's own "not geolocated" value. No address field is
        # written either, because `base_geolocalize.write()` resets both coordinates
        # to (0,0) when an address field moves without them.
        cls.unlocated = cls.env['res.partner'].create({'name': 'Call-site un-geocoded partner'})

    def _assert_names_the_unlocated_partner(self, error):
        """Every one of these raises must name the ADDRESS the operator has to go and
        fill in. A message that names only the provider, or only a count, sends them
        to the wrong place - that is the whole reason the raise is kept."""
        self.assertIn(
            self.unlocated.display_name, str(error),
            "the refusal does not name the un-geocoded address. An operator told only "
            "that 'routing failed' has no way to find which address to fix, which is "
            "the entire justification for keeping this a raise instead of degrading it "
            "to a silent status",
        )

    # ------------------------------------------------------------------
    # site 1 - res_partner.py:66
    # ------------------------------------------------------------------
    def test_haversine_distance_refuses_an_ungeocoded_endpoint(self):
        """RED against the PR base - delivery evidence for commit ``e79e9fc3c6``, not
        behaviour that already ships."""
        with self.assertRaises(ValidationError) as ctx:
            self.env['res.partner'].haversine_distance(self.located, self.unlocated)
        self._assert_names_the_unlocated_partner(ctx.exception)

    # ------------------------------------------------------------------
    # site 2 - res_partner.py:97
    # ------------------------------------------------------------------
    def test_rhumline_distance_refuses_an_ungeocoded_endpoint(self):
        """RED against the PR base - delivery evidence for commit ``e79e9fc3c6``, not
        behaviour that already ships.

        ``rhumline_distance`` has NO caller anywhere in this repository, in any of the
        five sibling provider modules, or in any other addons directory on the
        deployment's addons path (searched exhaustively from each repository root at
        close-out time; the only hits are its own definition and this suite). It is a
        PUBLIC, ``@api.model`` helper sitting next to ``haversine_distance``, which
        IS called - so it is an out-of-repository API surface, not dead code, and it
        is deliberately NOT deleted here. Being uncalled is exactly why it needs its
        own test: nothing else in the tree would notice if its validation were
        removed, and it has already carried one silent defect (it used to validate
        the origin twice and never inspect the destination).
        """
        with self.assertRaises(ValidationError) as ctx:
            self.env['res.partner'].rhumline_distance(self.located, self.unlocated)
        self._assert_names_the_unlocated_partner(ctx.exception)

    # ------------------------------------------------------------------
    # site 3 - geo_direction.py:44
    # ------------------------------------------------------------------
    def test_haversine_matrix_refuses_an_ungeocoded_partner(self):
        """RED against the PR base - delivery evidence for commit ``e79e9fc3c6``, not
        behaviour that already ships.

        Reached from the ``Get Distance Matrix`` button (``route_route_views.xml:55``)
        and from the self-heal sweep. Until now NO test drove this site at all.
        """
        partners = self.located | self.other_located | self.unlocated
        with self.assertRaises(ValidationError) as ctx:
            self.env['geo.direction']._compute_haversine_matrix(partners, 'driving')
        self._assert_names_the_unlocated_partner(ctx.exception)

    # ------------------------------------------------------------------
    # site 4 - geo_direction_manual.py:60
    # ------------------------------------------------------------------
    def test_manual_directions_refuse_an_ungeocoded_endpoint(self):
        """RED against the PR base - delivery evidence for commit ``e79e9fc3c6``, not
        behaviour that already ships.

        The manual provider is the one every database has enabled by default, so this
        is the site the ``Get Directions`` button reaches first. Until now NO test
        drove it directly - it was only ever reached transitively.
        """
        with self.assertRaises(ValidationError) as ctx:
            self.env['geo.direction.manual']._get_directions(self.located, self.unlocated, 'driving')
        self._assert_names_the_unlocated_partner(ctx.exception)

    def test_manual_directions_refuse_an_ungeocoded_intermediate_waypoint(self):
        """RED against the PR base, and the variant a two-endpoint test silently misses.

        ``all_points`` is ``origin + waypoints + destination``: a route whose two ENDS
        are both geolocated but whose middle stop is not must still be refused, and a
        test that only ever passes two partners would never notice if the waypoints
        were dropped from that expression.
        """
        with self.assertRaises(ValidationError) as ctx:
            self.env['geo.direction.manual']._get_directions(
                self.located, self.other_located, 'driving', waypoints=self.unlocated,
            )
        self._assert_names_the_unlocated_partner(ctx.exception)

    # ------------------------------------------------------------------
    # site 5 - geo_direction_manual.py:171
    # ------------------------------------------------------------------
    def test_manual_distance_matrix_refuses_an_ungeocoded_partner(self):
        """RED against the PR base - delivery evidence for commit ``e79e9fc3c6``, not
        behaviour that already ships.

        Two located partners are supplied alongside the un-geocoded one so the
        refusal cannot be confused with the ``len(partners) < 2`` guard that sits
        immediately above it in the same method.
        """
        partners = self.located | self.other_located | self.unlocated
        with self.assertRaises(ValidationError) as ctx:
            self.env['geo.direction.manual']._get_distance_matrix(partners, 'driving')
        self._assert_names_the_unlocated_partner(ctx.exception)

    # ------------------------------------------------------------------
    # THE PRE-FILTER that keeps site 1 off the plain-read path
    # ------------------------------------------------------------------
    def test_reading_a_section_with_an_ungeocoded_endpoint_does_not_raise(self):
        """RED against the PR base - and the single most important thing in this file to
        keep green.

        RED FOR A DIFFERENT REASON THAN ITS SIX SIBLINGS, which is why it gets its own
        note. The others are red at the base because the raise they assert never fires.
        This one asserts the ABSENCE of a raise, so that mechanism cannot be what makes
        it red. It is red at ``origin/18.0`` for two independent reasons:

        1. ``_compute_distance`` at the base guards with
           ``_validate_coordinates(raise_if_not_valid=False)``, whose presence branch is
           the dead ``is False`` test. An un-geocoded endpoint therefore returns True,
           the compute takes the ``else`` branch, and ``distance`` is set to a real
           haversine measured FROM NULL ISLAND - so ``assertFalse(values[0]['distance'])``
           fails on a non-zero number. That wrong-distance-instead-of-blank result is
           precisely the defect commit ``e79e9fc3c6`` fixed.
        2. ``distance_status`` does not exist on the model at the base at all, so the
           ``read()`` below cannot even be issued there.

        The ``_has_measurable_endpoints()`` pre-filter this docstring describes is also
        absent at the base; it arrived with the same commit.

        ``res_partner.py:66`` is reached from inside the ``_compute_distance`` COMPUTE
        (``route_section_mixin.py:106``) - but only on the ``else`` branch, after the
        NON-raising ``_has_measurable_endpoints()`` pre-filter. That pre-filter is the
        only thing standing between an un-geocoded address and a ``ValidationError``
        on an ordinary list-view read: a compute that raises makes the record
        UNREADABLE, so every list and every form showing it would break, for every
        user, with no way to reach the address and fix it.

        Removing or "simplifying" that pre-filter while touching the raise at
        ``:66`` is therefore not a refactor - it converts a targeted, actionable
        refusal on a button press into an unreadable record. This test drives a plain
        read of exactly such a section and asserts it comes back quietly with a blank
        distance.
        """
        section = self.env['route.section'].create({
            'address_from_id': self.located.id,
            'address_to_id': self.unlocated.id,
        })
        self.env.flush_all()
        section.invalidate_recordset()

        # A plain read - the shape a list view issues - must not raise.
        values = section.read(['name', 'distance', 'distance_status'])

        self.assertEqual(len(values), 1, "precondition failed: expected exactly one row back")
        self.assertFalse(
            values[0]['distance'],
            "a section whose endpoint carries no usable coordinates must read a BLANK "
            "distance, not a measured one",
        )
        self.assertEqual(
            values[0]['distance_status'], 'no_coordinates',
            "the blank distance must come with the cause an operator can act on - a "
            "blank distance with no recorded reason is the ambiguity distance_status "
            "exists to remove",
        )
