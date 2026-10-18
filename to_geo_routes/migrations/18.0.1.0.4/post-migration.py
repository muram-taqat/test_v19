import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """
    Populate ``route.section.transport.mode.distance_status`` on rows that
    predate the field (Viindoo/tvtmaaddons#14409).

    ``distance_status`` exists because ``distance`` cannot answer the question
    an operator actually asks about a section reading 0 km: was this route
    MEASURED as zero, or NEVER MEASURED at all? ``distance`` is a
    ``fields.Float`` with no ``digits=`` at any of its six declaration sites in
    this module, so ``Float.convert_to_column`` stores ``float(value or 0.0)``
    and ``Float.convert_to_record`` returns ``value or 0.0`` - the two states
    are one stored value and no attribute read and no search domain can
    separate them.

    **Why a script at all, given the field is computed.** Adding a stored
    computed column makes Odoo recompute it for every existing row
    (``Field.update_db`` returns True for a new column, ``_auto_init`` queues
    it, ``Registry.init_models`` flushes it - all of that finishes BEFORE this
    script runs, per ``odoo/modules/loading.py`` and ``odoo/modules/registry.py``).
    That recompute (``_compute_distance_status``) answers ONLY from the two
    endpoints' coordinate validity via ``_has_measurable_endpoints()`` - it gets
    ``no_coordinates`` exactly right for a row with no usable endpoint, and this
    script must never re-litigate that. What it CANNOT know is, among the rows
    that DO have valid coordinates, whether a distance was ever actually
    resolved: it stamps every one of them ``measured``, including a row whose
    coordinates are valid today but that has never been fetched from a provider
    or computed at all (the false positive the field was invented to remove -
    see Control D in the test coverage). This script adds exactly that missing
    refinement, and only within the "has valid coordinates" subset the compute
    already carved out.

    **The derivation, and its limit.** A positive historical ``distance`` proves
    a measurement was once resolved - by the haversine compute, by a provider
    fetch, or by a human typing one in - so, AMONG rows with usable endpoint
    coordinates, those rows are ``measured``. But ``distance > 0`` alone is NOT
    sufficient on its own: a row whose endpoint has since lost or never had
    usable coordinates can still carry a leftover positive ``distance`` from
    before coordinate validation was tightened (``_validate_coordinates`` now
    rejects Null Island, (0.0, 0.0), which it used to accept), and stamping such
    a row ``measured`` would plant a false measurement rather than surface the
    unresolved endpoint #14409 exists to expose. So endpoint validity is decided
    FIRST, unconditionally, via ``_has_measurable_endpoints()`` -
    ``route_section_mixin.py``'s single spelling of the routing-endpoint
    precondition, shared with ``_compute_distance``/``_compute_distance_status``/
    ``_get_distance_matrix`` - and only rows that PASS it are then split by
    their historical ``distance``. A row that FAILS it gets ``no_coordinates``
    instead - a DETERMINED cause, never the generic default below - and is
    excluded from the ``distance`` split entirely. Only the remainder of that
    split - valid coordinates, but a distance of ``0.0``/NULL - has no
    recoverable historical cause and gets ``no_provider_data``, the field's
    own generic "not determined" default. This ordering also means a row
    `init_models` already correctly computed ``no_coordinates`` for is never
    overwritten with the generic default, protecting the same downgrade
    ``_mark_generic_distance_status``'s ``DETERMINED_DISTANCE_STATUS_CAUSES``
    guard (``route_section_transport_mode.py``) protects against elsewhere in
    the module. From this release forward the cause IS recorded at the moment
    it happens, by the compute and by the two sweeps in
    ``models/route_section_transport_mode.py``; this script only classifies the
    historical backlog.

    **Why a full-table ``search([])`` plus Python-side ``.filtered()``, not an
    SQL-shaped domain.** ``_has_measurable_endpoints()`` is deliberately not
    re-expressed as a domain here - its own docstring names three call sites
    that must all agree on the routing-endpoint precondition, and a
    fourth, SQL-shaped spelling in this migration is exactly the drift it
    forbids. One ``search([])`` plus two in-memory ``.filtered()`` passes is a
    single SELECT for a one-shot script that runs exactly once on upgrade, and
    matches the idiom the 18.0.1.0.3 migration in this same module already
    uses (``search()`` + ``.filtered()`` with a coordinate predicate); no
    batching via ``self.env['to.base'].splittor`` is warranted for a one-time
    classification pass with no per-row external call.

    ``active_test=False`` is deliberate, for the same reason the 18.0.1.0.3
    migration gives: ``route.section.transport.mode.active`` is a real stored
    field related to ``transport_mode_provider_id.active``, so a row sitting
    under an archived provider would otherwise be silently skipped and left
    holding whatever the recompute stamped on it. This script runs exactly
    once and there is no re-runner to catch it later.

    Odoo only runs migration scripts for the 'to upgrade' module state, never
    'to install', so a fresh install never reaches this script - it has no
    historical rows to classify. The ``version`` guard below is defensive
    only, for a hypothetical manual invocation with ``version=None``.
    """
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    RouteSectionMode = env['route.section.transport.mode'].with_context(active_test=False)

    all_modes = RouteSectionMode.search([])

    # Ground truth first: a row without two usable endpoint coordinates has
    # no measurement at all, whatever a stale `distance` column still holds.
    # THE single spelling of the routing-endpoint precondition
    # (route_section_mixin.py `_has_measurable_endpoints`) - never re-expressed
    # as a domain here, see docstring.
    no_coordinates = all_modes.filtered(lambda mode_record: not mode_record._has_measurable_endpoints())
    if no_coordinates:
        no_coordinates.write({'distance_status': 'no_coordinates'})

    # Among the REMAINING rows - the ones that do carry usable coordinates -
    # a resolved distance is the one fact the historical data still carries.
    remaining = all_modes - no_coordinates
    measured = remaining.filtered(lambda mode_record: mode_record.distance > 0.0)
    if measured:
        measured.write({'distance_status': 'measured'})

    # Everything else remaining: valid coordinates, but no distance was ever
    # actually resolved. The historical cause is not recoverable from the
    # data, so this gets the field's own generic "not determined" default.
    no_provider_data = remaining - measured
    if no_provider_data:
        no_provider_data.write({'distance_status': 'no_provider_data'})

    _logger.info(
        "to_geo_routes migration 18.0.1.0.4: classified %s route.section.transport.mode "
        "record(s) as 'no_coordinates', %s as 'measured' and %s as 'no_provider_data' "
        "(historical cause not recoverable); the cause is recorded at write time from "
        "this release on.",
        len(no_coordinates),
        len(measured),
        len(no_provider_data),
    )

    # The writes above are on a stored COMPUTED field with no inverse: they
    # survive only until `_compute_distance_status`'s dependencies move, which
    # is the shared lifecycle `distance` itself has and exactly what is wanted.
    # Flush so this one-shot script persists them - there is no later request
    # cycle to do it lazily.
    env.flush_all()
