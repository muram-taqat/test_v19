import os
from glob import glob

from odoo import release
from odoo.modules.migration import load_script
from odoo.modules.module import get_manifest, get_module_path
from odoo.tests import TransactionCase, tagged
from odoo.tools.parse_version import parse_version

MODULE_NAME = 'to_geo_routes'

# Odoo scans both names for upgrade scripts (odoo/modules/migration.py:_get_files).
MIGRATION_DIRECTORIES = ('migrations', 'upgrades')

# Odoo hands migrate() the version that was INSTALLED before the upgrade. This is the
# version to_geo_routes shipped immediately before 18.0.1.0.4 (the script under test),
# per EXISTING COVERAGE / test_route_section_cache.py, which already owns 18.0.1.0.3.
PREVIOUS_RELEASED_VERSION = '18.0.1.0.3'


def full_version(version):
    """Series-prefix a bare module version, mirroring ``migration.convert_version``."""
    if version == '0.0.0' or version.count('.') > 2:
        return version
    return '%s.%s' % (release.major_version, version)


@tagged('post_install', '-at_install')
class TestMigrationDistanceStatusBackfill(TransactionCase):
    """Protects the `18.0.1.0.4` post-migration script that backfills
    `route.section.transport.mode.distance_status` on pre-existing rows
    (Viindoo/tvtmaaddons#14409).

    ## Why this file exists, and why it is not `test_distance_status.py`

    `test_distance_status.py` owns the FIELD's own behaviour - what
    `_compute_distance_status` and the two sweeps in
    `route_section_transport_mode.py` do. It never touches a migration script.
    `test_route_section_cache.py` owns migration `18.0.1.0.3`. Migration
    `18.0.1.0.4` had zero coverage before this file - that is the gap this file
    fills, by importing the script directly and calling `migrate()`, exactly the
    way `viin_logistics/tests/test_port_coordinate_migration.py` already does for
    its own module.

    ## The defect this file demonstrates

    `18.0.1.0.4/post-migration.py` runs AFTER Odoo's `init_models` has already
    computed the CORRECT `distance_status` for every pre-existing row purely from
    endpoint coordinate validity (`odoo/models.py` queues every existing
    row for computation the moment a new stored computed column appears). The
    script then OVERWRITES that correct answer with its own heuristic, derived
    from the stale `distance` column alone:

      - `('distance', '>', 0.0)` -> `'measured'`
      - `('distance', 'in', [False, 0.0])` -> `'no_provider_data'`

    Both searches are exhaustive, unconditional, and blind to `_has_measurable_endpoints()`.
    Two concrete wrong outcomes follow, both protected below:

      - **Defect A** (`test_stale_positive_distance_from_a_null_island_endpoint_is_not_stamped_measured`):
        a row whose endpoint carries NO usable coordinates, but whose stale
        `distance` column still holds a leftover positive number from before
        `_validate_coordinates` rejected (0.0, 0.0), gets stamped `'measured'` -
        a false measurement landed on a customer database.
      - **Defect B** (`test_uncoordinated_endpoint_row_is_not_downgraded_from_no_coordinates_to_no_provider_data`):
        a row correctly computed `'no_coordinates'` by `init_models`, whose
        `distance` is 0/NULL, gets downgraded to the generic `'no_provider_data'`
        default - destroying a DETERMINED cause
        (`DETERMINED_DISTANCE_STATUS_CAUSES`, `route_section_mixin.py`) that
        `_mark_generic_distance_status` (`route_section_transport_mode.py`)
        exists specifically to protect from exactly this kind of downgrade.

    Cases C, D and E are CONTROLS - they must stay green both before and after any
    fix, and are what stops a fix from over-correcting onto rows the bug never
    touched.

    ## The fixture trap (read before editing a fixture in this file)

    `distance` is a STORED COMPUTED field (`distance.mixin` declares it stored,
    `route.section.mixin` adds `compute='_compute_distance'` and the coordinate
    `depends`, and the transport-mode redeclaration changes only `help=`, so
    compute/store/depends carry down the `_inherit` chain unchanged - see the
    class docstring of `test_distance_status.py` for the full derivation). Blanking
    an endpoint's coordinates through the ORM therefore RE-FIRES the compute and
    blanks `distance` right back to False - a naive attempt to build "endpoint with
    no coordinates but a stale positive distance" destroys itself. Defect A's
    fixture instead materialises the historical column state directly with a raw
    `UPDATE route_section_transport_mode SET distance = ...` after the endpoint is
    already set to (0.0, 0.0), followed by `invalidate_recordset(['distance'])`.
    This is legitimate and necessary here, not a shortcut to "clean up" later: it
    reproduces a column state the CURRENT code can no longer produce (coordinate
    validation now rejects Null Island before a distance is ever computed from it)
    but that exists on every customer database that predates that validation
    tightening.

    Also note what `_build_section` below does NOT need help with: creating a
    `route.section` cascades into `_generate_route_section_transport_modes_if_not_exists`,
    which itself calls `_update_distance_ave_speed()` over the freshly created rows
    (`route_section.py`). For the manual provider that resolves a REAL
    haversine distance immediately for any row with valid endpoints (`'measured'`,
    a genuinely positive `distance`) and leaves an invalid-endpoint row exactly at
    `distance_status='no_coordinates'`, `distance=False` - which is precisely the
    `init_models`-equivalent precondition every test below asserts before ever
    touching the migration script.

    ## The second trap - never assert on the falsiness of `distance`

    `distance` is a `fields.Float` with no `digits=`, so `Float.convert_to_column`
    stores `float(value or 0.0)` and `Float.convert_to_record` returns
    `value or 0.0` - writing `False` STORES `0.0`, and a NULL column READS `0.0`.
    `assertFalse(row.distance)` is green before and after any fix and proves
    nothing; every "was this measured" assertion in this file goes through
    `distance_status`, never through `distance`'s truthiness. See
    `test_distance_status.py`'s class docstring for the long-form version of this
    warning - it applies unchanged to migration fixtures.

    ## Relationship to the existing suite

    This file neither imports nor subclasses `TestDistanceStatus`,
    `TestRouteSectionCache`, `TestCommon`, or any other test class in this module -
    subclassing an Odoo `TestCase` re-runs its entire suite, a trap this module's
    own suite already documents in several places. The fixture helpers below are a
    deliberate, minimal re-implementation of the same idea `test_distance_status.py`
    already uses (`_make_partner`, `_build_section`, `_cache_row`), each scoped with
    brand-new `res.partner` records per test so no fixture here can collide with
    demo data or another test's addresses on the `address_from_address_to_unique`
    constraint.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.manual_driving_provider = cls.env.ref('to_geo_routes.manual_input_driving')
        cls.manual_rail_provider = cls.env.ref('to_geo_routes.manual_input_rail')

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked for -
        omitting both stores SQL NULL (reads back 0.0); passing 0.0 explicitly
        stores a real (0.0, 0.0) - Null Island. No address field is ever passed:
        `base_geolocalize.write()` resets both coordinates to (0, 0) whenever an
        address field is written without both coordinates in the same vals."""
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    def _build_section(self, address_from, address_to):
        """Create a `route.section` directly - the same shape `data/data_demo.xml`
        uses. `create` cascades into `_generate_route_section_transport_modes_if_not_exists`,
        which creates one cache row per `transport.mode.provider` and runs
        `_update_distance_ave_speed` over them (see class docstring)."""
        return self.env['route.section'].create({
            'address_from_id': address_from.id,
            'address_to_id': address_to.id,
        })

    def _cache_row(self, section, mode='driving'):
        return section.transport_mode_ids.filtered(
            lambda mode_record: mode_record.provider == 'manual' and mode_record.mode == mode
        )

    # ------------------------------------------------------------------
    # migration plumbing
    # ------------------------------------------------------------------
    def _post_migration_scripts(self):
        """``[(directory version, absolute path)]`` for every post-migration script, oldest first."""
        module_path = get_module_path(MODULE_NAME)
        found = []
        for directory in MIGRATION_DIRECTORIES:
            pattern = os.path.join(module_path, directory, '*', 'post-migration.py')
            for path in glob(pattern):
                found.append((os.path.basename(os.path.dirname(path)), path))
        return sorted(found, key=lambda item: parse_version(full_version(item[0])))

    def _run_post_migration(self):
        """Load and execute the NEWEST shipped post-migration script - `18.0.1.0.4`,
        the one under test - against the current transaction, then invalidate this
        env's cache so subsequent reads see what `migrate()` wrote through its own
        (separate) Environment."""
        scripts = self._post_migration_scripts()
        self.assertTrue(scripts, "arrange: %s ships no post-migration script." % MODULE_NAME)
        script_path = scripts[-1][1]
        script = load_script(script_path, 'to_geo_routes_post_migration_under_test')
        self.assertTrue(
            hasattr(script, 'migrate'),
            "%s must define migrate(cr, version) - Odoo's upgrade driver calls nothing "
            "else (odoo/modules/migration.py)." % script_path,
        )
        # Everything the fixture wrote must be visible to migrate()'s raw SQL
        # searches, which read the actual table, not this env's Python cache.
        self.env.flush_all()
        script.migrate(self.env.cr, PREVIOUS_RELEASED_VERSION)
        self.env.invalidate_all()

    # ------------------------------------------------------------------
    # Defect A - false 'measured' from a stale positive distance
    # ------------------------------------------------------------------
    def test_stale_positive_distance_from_a_null_island_endpoint_is_not_stamped_measured(self):
        """Business rule: a route section whose endpoint carries NO usable
        coordinates must never be reported 'measured'. An operator who sees
        'measured' trusts the number; stamping it here plants a false measurement
        on a customer's database and hides exactly the row #14409 exists to
        surface.

        RED evidence expected on the unmodified tree: the final assertion fails
        because the migration's unconditional `('distance', '>', 0.0)` sweep
        classifies this row 'measured'.
        """
        null_island = self._make_partner('Defect-A Null Island endpoint', latitude=0.0, longitude=0.0)
        real_endpoint = self._make_partner('Defect-A real endpoint', latitude=21.010458, longitude=105.787416)
        section = self._build_section(null_island, real_endpoint)
        row = self._cache_row(section)
        self.assertTrue(row, "precondition failed: expected a manual/driving cache row")

        # PRECONDITION - this is exactly the state a real upgrade's init_models
        # leaves BEFORE the migration script ever runs: the row's own compute
        # already got this right.
        self.assertEqual(
            row.distance_status, 'no_coordinates',
            "arrange: an endpoint with no usable coordinates must compute "
            "'no_coordinates' before the migration ever touches this row - if this "
            "fails, the fixture itself is wrong, not the migration.",
        )

        # Materialise the historical column state a pre-fix database carries: a
        # leftover positive distance from before coordinate validation tightened.
        # See the class docstring for why this must be raw SQL.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE route_section_transport_mode SET distance = %s WHERE id = %s",
            (12.5, row.id),
        )
        row.invalidate_recordset(['distance'])
        self.assertEqual(
            row.distance, 12.5,
            "arrange: the stale historical distance must read back positive - if this "
            "fails the raw UPDATE did not take, and the test is not exercising Defect A",
        )

        self._run_post_migration()

        self.assertEqual(
            row.distance_status, 'no_coordinates',
            "Business rule: a route section whose endpoint carries no usable "
            "coordinates must never be classified 'measured' merely because a "
            "leftover positive `distance` survives from before coordinate "
            "validation tightened. The migration's `('distance', '>', 0.0)` sweep "
            "stamps 'measured' on exactly this shape of row today, planting a "
            "false measurement on a customer database and hiding the row #14409 "
            "exists to surface. Got distance_status=%r (row id %s)."
            % (row.distance_status, row.id),
        )

    # ------------------------------------------------------------------
    # Defect B - the R2 downgrade: a determined cause overwritten by the
    #            generic undetermined default
    # ------------------------------------------------------------------
    def test_uncoordinated_endpoint_row_is_not_downgraded_from_no_coordinates_to_no_provider_data(self):
        """Business rule: a row `init_models` already correctly classified
        'no_coordinates' - a DETERMINED cause an operator can act on (go fix the
        endpoint) - must never be downgraded to the generic 'no_provider_data'
        default, which tells the operator nothing. R2
        (`route_section_transport_mode.py`, `DETERMINED_DISTANCE_STATUS_CAUSES`,
        `route_section_mixin.py`) exists specifically to forbid this class of
        downgrade elsewhere in the module; the migration script bypasses it
        entirely by writing `distance_status` directly.

        RED evidence expected on the unmodified tree: the final assertion fails
        because the migration's `('distance', 'in', [False, 0.0])` sweep
        classifies this row 'no_provider_data'.
        """
        null_island = self._make_partner('Defect-B Null Island endpoint', latitude=0.0, longitude=0.0)
        real_endpoint = self._make_partner('Defect-B real endpoint', latitude=20.861135, longitude=106.672549)
        section = self._build_section(null_island, real_endpoint)
        row = self._cache_row(section)
        self.assertTrue(row, "precondition failed: expected a manual/driving cache row")

        # PRECONDITION - no raw SQL needed here: section creation's own
        # `_update_distance_ave_speed` sweep already leaves this row exactly where
        # a real upgrade's init_models would, distance unresolved (0/NULL is one
        # stored value on this Float - see class docstring) and the cause recorded.
        self.assertEqual(
            row.distance_status, 'no_coordinates',
            "arrange: an endpoint with no usable coordinates must compute "
            "'no_coordinates' before the migration ever touches this row - if this "
            "fails, the fixture itself is wrong, not the migration.",
        )

        self._run_post_migration()

        self.assertEqual(
            row.distance_status, 'no_coordinates',
            "Business rule: the migration must never downgrade a DETERMINED cause "
            "('no_coordinates') to the generic 'no_provider_data' default - doing "
            "so on a customer database erases the one fact an operator could act "
            "on (fill in the endpoint's coordinates) and replaces it with a dead "
            "end that looks identical to every other unresolved row. Got "
            "distance_status=%r (row id %s)."
            % (row.distance_status, row.id),
        )

    # ------------------------------------------------------------------
    # Control C - a genuinely measured row must stay 'measured'
    # ------------------------------------------------------------------
    def test_control_measured_row_with_valid_endpoints_stays_measured(self):
        """CONTROL - must be green both before and after any fix. Two valid,
        distinct endpoints yield a real positive haversine distance; the
        migration's `('distance', '>', 0.0)` sweep is the historically correct
        classification for this shape of row and must not be disturbed by
        whatever guards a fix adds for Defect A/B.
        """
        origin = self._make_partner('Control-C origin', latitude=21.010458, longitude=105.787416)
        destination = self._make_partner('Control-C destination', latitude=20.861135, longitude=106.672549)
        section = self._build_section(origin, destination)
        row = self._cache_row(section)
        self.assertTrue(row, "precondition failed: expected a manual/driving cache row")

        self.assertEqual(
            row.distance_status, 'measured',
            "arrange: two valid endpoints must compute 'measured' before the "
            "migration ever touches this row.",
        )
        self.assertGreater(
            row.distance, 0.0,
            "arrange: two distinct real coordinates must yield a positive haversine "
            "distance, or this control does not exercise the 'measured' bucket at all.",
        )

        self._run_post_migration()

        self.assertEqual(
            row.distance_status, 'measured',
            "control: a genuinely measured row (positive distance, valid endpoints) "
            "must stay 'measured' after the migration runs. Got distance_status=%r "
            "(row id %s) - if this is red, the model of the defect in this file is "
            "wrong and must be revisited before anything else." % (row.distance_status, row.id),
        )

    # ------------------------------------------------------------------
    # Control D - an unresolved row with valid endpoints stays the generic default
    # ------------------------------------------------------------------
    def test_control_unresolved_row_with_valid_endpoints_stays_no_provider_data(self):
        """CONTROL - must be green both before and after any fix. Both endpoints
        are valid, so `init_models` would have stamped this row 'measured' even
        though no distance was ever actually resolved for it - precisely the false
        positive `distance_status` was invented to eliminate (see the migration
        script's own docstring, `post-migration.py`). The migration's
        `('distance', 'in', [False, 0.0])` sweep correctly overrides that false
        'measured' with 'no_provider_data' here, and any fix for Defect A/B - which
        is about rows with INVALID endpoints - must not touch this row.
        """
        origin = self._make_partner('Control-D origin', latitude=20.861135, longitude=106.672549)
        destination = self._make_partner('Control-D destination', latitude=20.9522972, longitude=107.0781387)
        section = self._build_section(origin, destination)
        row = self._cache_row(section)
        self.assertTrue(row, "precondition failed: expected a manual/driving cache row")
        self.assertEqual(
            row.distance_status, 'measured',
            "arrange: two valid endpoints must compute 'measured' regardless of "
            "whether a distance was ever actually resolved - this is exactly what "
            "a real upgrade's init_models leaves on this class of row.",
        )

        # Blank the distance the way the module's own self-heal paths do
        # (`remain.distance = False`, `missing_records.write({'distance': False})`),
        # simulating a row that was never actually resolved by any provider.
        row.write({'distance': False})
        self.env.flush_all()
        self.assertEqual(
            row.distance_status, 'measured',
            "arrange: blanking `distance` alone must not retrigger "
            "`_compute_distance_status` - it depends only on the coordinate "
            "fields, not on `distance` - so the row stays exactly where "
            "init_models left it.",
        )

        self._run_post_migration()

        self.assertEqual(
            row.distance_status, 'no_provider_data',
            "control: a row with valid endpoints but no resolved distance must be "
            "classified 'no_provider_data' by the migration - the exact case the "
            "script's own historical-derivation heuristic exists to handle "
            "correctly. Got distance_status=%r (row id %s)." % (row.distance_status, row.id),
        )

    # ------------------------------------------------------------------
    # Control E - a row under an ARCHIVED provider must still be reached
    # ------------------------------------------------------------------
    def test_control_row_under_an_archived_provider_is_still_reached_by_the_migration(self):
        """CONTROL, not a defect demonstration - guards an existing invariant, not
        a bug this file is reporting. `route.section.transport.mode.active` is a
        real stored field related to `transport_mode_provider_id.active`
        (`route_section_transport_mode.py`), so the migration explicitly uses
        `.with_context(active_test=False)` - a row sitting under an archived
        provider must still be swept, exactly like the 18.0.1.0.3 migration this
        script's own docstring cites as precedent. This runs exactly once, with no
        re-runner to catch a silently skipped row later.

        A canary value is written before archiving so a skip (missing
        `active_test=False`) and a real classification are distinguishable: the
        canary is a legal `distance_status` value the migration would never itself
        choose for this row, so its survival past `migrate()` is proof of a skip.
        """
        origin = self._make_partner('Control-E origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('Control-E destination', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, destination)
        row = self._cache_row(section, mode='rail')
        self.assertTrue(row, "precondition failed: expected a manual/rail cache row")
        self.assertEqual(
            row.distance_status, 'measured',
            "arrange: two valid endpoints must compute 'measured' before archiving "
            "the provider, matching what a real upgrade's init_models would leave "
            "on this row too.",
        )

        # Blank distance the same way Control D does, so this row lands in the
        # 'undetermined' bucket the migration classifies as 'no_provider_data' -
        # the reachability question is orthogonal to which bucket the row falls in.
        row.write({'distance': False})

        row.transport_mode_provider_id.active = False
        row.invalidate_recordset(['active'])
        self.assertFalse(
            row.active,
            "arrange: the cache row's own `active` must mirror its archived "
            "provider (route_section_transport_mode.py) - if this fails, "
            "the fixture is not exercising an archived provider at all.",
        )

        # Canary - see docstring. Written directly, mirroring the exact shape
        # `_mark_generic_distance_status` itself uses in production code.
        row.distance_status = 'no_time'
        self.env.flush_all()

        self._run_post_migration()

        self.assertEqual(
            row.distance_status, 'no_provider_data',
            "control: a route section under an ARCHIVED provider must still be "
            "swept and classified by the migration, not silently skipped. Finding "
            "the pre-migrate canary ('no_time') surviving here would mean the "
            "migration silently skips every row under any archived or disabled "
            "provider integration on a customer database - a much larger blast "
            "radius than one deliberately-canaried test row. Got "
            "distance_status=%r (row id %s)." % (row.distance_status, row.id),
        )

    # ------------------------------------------------------------------
    # Regression guard - unreachable migration directories are silently dead
    # ------------------------------------------------------------------
    def test_every_shipped_migration_is_reachable_from_the_declared_module_version_regression_guard(self):
        """GREEN today - a regression guard, not a defect demonstration.

        A migration directory above the manifest version never executes.
        `odoo/modules/migration.py` runs a script only when
        `installed_version < directory_version <= current_version`, where
        `current_version` is the manifest version series-prefixed by
        `convert_version` (`1.0.4` -> `18.0.1.0.4`). So a `migrations/18.0.1.0.5/`
        directory shipped while the manifest still declares `1.0.4` would be dead
        code: skipped in silence, on every database, forever. Every other test in
        this file would stay green regardless, because they import the script
        directly - only this test notices Odoo will never call it.

        Confirmed today: manifest declares `1.0.4`, the highest shipped directory
        is `18.0.1.0.4` - reachable.
        """
        scripts = self._post_migration_scripts()
        self.assertTrue(scripts, "arrange: no migration script to check reachability for.")

        declared_version = get_manifest(MODULE_NAME)['version']
        current_version = parse_version(full_version(declared_version))
        unreachable = sorted(
            version for version, _path in scripts
            if not parse_version(full_version(version)) <= current_version
        )
        self.assertEqual(
            unreachable, [],
            "Migration director%s %s %s above the declared module version %r, so "
            "Odoo skips %s silently (odoo/modules/migration.py requires "
            "directory_version <= current_version). Bump the manifest 'version' to "
            "at least the highest migration directory."
            % ('y' if len(unreachable) == 1 else 'ies', ', '.join(unreachable),
               'is' if len(unreachable) == 1 else 'are', declared_version,
               'it' if len(unreachable) == 1 else 'them'),
        )
