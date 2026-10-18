import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """
    Repair ``route.section.transport.mode`` cache rows corrupted by write
    paths this same release closes (Viindoo/tvtmaaddons#14279): the
    previously unguarded ``route.segment._inverse_distance_ave_speed``
    write-back (now only pushes back when the segment's own value differs
    from the section's), the provider-observation converter
    (``route.section.mixin._prepare_distance_vals_from_provider``, which no
    longer lets a degenerate zero-time observation zero out ``ave_speed``
    while ``distance`` stays positive), and the miles-field seeding at
    segment creation (``waypoint_mixin._prepare_segment_vals_list``).

    A corrupted row still carries the correct, module-computed ``distance``
    but its ``ave_speed`` was silently zeroed and persisted back into the
    source of truth. Once zeroed, the row becomes permanently unselectable
    by the module's own self-heal path (``_update_missing_distances_ave_speed``,
    ``models/route_section_transport_mode.py``), which searches on
    ``distance in (False, 0.0)`` - a corrupted row's distance is populated,
    so that search can never find it.

    Odoo only runs migration scripts for the 'to upgrade' module state,
    never 'to install', so a fresh install never reaches this script. The
    ``version`` guard below is defensive only, for a hypothetical manual
    invocation with ``version=None``: on a fresh install there is nothing
    to repair yet, since the corruption is a byproduct of the very bug
    this release fixes.

    Two further hazards, found by code review, are guarded against below:

    - A row whose OWN endpoint (``address_from_id``/``address_to_id``) now
      carries an invalid coordinate can never be resolved by
      ``_update_distance_ave_speed()``. Calling it anyway is worse than a
      no-op: ``_update_distance_ave_speed`` ends with
      ``remain = self - processed; if remain: remain.distance = False``, so
      an unresolvable row's still-correct ``distance`` would be destroyed.
      Such rows are excluded from the repair call entirely and their
      ``distance`` is left untouched - still corrupted-but-distance-intact.
      There is no automatic re-runner today: nothing currently calls
      ``_update_missing_distances_ave_speed`` (no ``ir.cron`` exists in the
      ``to_geo_routes`` family), so such a row only becomes eligible for
      repair again if that method is invoked by some future caller - unlike
      this migration (which runs exactly once and would otherwise blank the
      row's distance for good).
    - The default ``search()`` uses ``active_test=True``, and
      ``route.section.transport.mode.active`` is a real stored field
      (related to ``transport_mode_provider_id.active``), so a corrupted row
      sitting under an archived provider would otherwise be silently
      skipped. This migration is version-gated and runs exactly once, and
      (as above) there is currently no automatic re-runner to catch a row
      missed here later - so the search explicitly includes archived
      providers.
    """
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Corrupted iff: provider is manual (an API-provider row legitimately
    # sits at ave_speed=0 while unresolved and must be left alone - it
    # refills on the next API call), distance is positive (proves the row
    # WAS resolved by _update_distance_ave_speed - the manual provider's
    # matrix always carries a positive speed, so "distance populated,
    # speed zero" is otherwise unreachable by the normal path), and
    # ave_speed is exactly zero (the corruption fingerprint). A single
    # search() call is one SQL SELECT - never a per-record mapped() loop
    # over the whole table. `active_test=False` includes rows sitting under
    # an archived provider - see docstring.
    corrupted = env['route.section.transport.mode'].with_context(active_test=False).search([
        ('provider', '=', 'manual'),
        ('ave_speed', '=', 0.0),
        ('distance', '>', 0.0),
    ])

    # Narrow to the rows this migration can actually repair: both endpoints
    # must carry an IN-RANGE coordinate. A row failing this check is left
    # exactly as found - still corrupted, but its distance intact - rather
    # than destroyed by _update_distance_ave_speed's `remain.distance = False`
    # tail.
    #
    # Deliberately `_check_coordinate_range`, NOT `_validate_coordinates`.
    # This migration already shipped, so its repairable/skipped split must
    # stay bit-for-bit identical on any database that has not yet run it.
    # `_validate_coordinates` was since recomposed to test coordinate
    # PRESENCE as well as range (a partner at (0,0) is now correctly reported
    # as un-geolocated), which would silently shrink the set of rows this
    # migration repairs. The range-only method preserves the original
    # behaviour exactly. Do NOT "modernise" this back to the composed
    # validator - the divergence from the live call sites in
    # route_section_mixin.py is intentional, not drift.
    repairable = corrupted.filtered(
        lambda r: r.address_from_id
        and r.address_to_id
        and r.address_from_id._check_coordinate_range(raise_if_not_valid=False)
        and r.address_to_id._check_coordinate_range(raise_if_not_valid=False)
    )
    skipped = corrupted - repairable

    if skipped:
        _logger.warning(
            "to_geo_routes migration 18.0.1.0.3: skipping %s corrupted "
            "route.section.transport.mode record(s) with an invalid "
            "endpoint coordinate - their distance is left untouched "
            "(id, route.section, address_from, address_to): %s",
            len(skipped),
            [
                (r.id, r.route_section_id.display_name, r.address_from_id.display_name, r.address_to_id.display_name)
                for r in skipped
            ],
        )

    if repairable:
        # Re-derive the correct values from the module's own provider-matrix
        # lookup - never a raw literal - so the restored value is bit-identical
        # to what a fresh install produces.
        repairable._update_distance_ave_speed()
        # `route.segment.distance`/`ave_speed` mirror this cache row but carry
        # NO `@api.depends` edge back to it (`route.segment._compute_distance`
        # / `_compute_ave_speed` depend only on `route_section_id` and
        # `route_id.transport_mode` - never on the cache row's own
        # distance/ave_speed - by design, since a declarative edge would fire
        # on every cache write and silently revert a hand-typed segment
        # override; see `_push_mirroring_segment_recompute`'s own docstring).
        # Repairing `repairable` alone therefore never dirties the segments
        # that mirror it. Explicitly queue them for recompute the same way
        # the provider-fetch path does, by reusing the module's own helper on
        # the just-repaired recordset - never a new `@api.depends` edge.
        repairable._push_mirroring_segment_recompute()
        _logger.info(
            "to_geo_routes migration 18.0.1.0.3: repaired %s corrupted "
            "route.section.transport.mode record(s): %s",
            len(repairable),
            repairable.ids,
        )
    else:
        _logger.info(
            "to_geo_routes migration 18.0.1.0.3: no repairable corrupted "
            "route.section.transport.mode records found."
        )

    # The mirroring `route.segment` rows were already queued for recompute
    # above via `_push_mirroring_segment_recompute()` (called from within
    # `_update_distance_ave_speed()` for the just-processed rows, and
    # explicitly again on `repairable` here) - `add_to_compute` only marks a
    # field dirty, it does not run the compute. `flush_all()` is what forces
    # Odoo to actually execute those queued recomputes (`route.segment`
    # `distance`/`ave_speed`, and anything depending on them such as
    # `route.route` totals) and persist the result before this one-shot
    # migration script ends - there is no later request cycle to do it
    # lazily.
    env.flush_all()
