from lxml import etree

from odoo.tests import TransactionCase, tagged

# What this file protects (Issue #14409, same root cause as `test_distance_status.py`,
# a DIFFERENT surface of it): `distance_status` is a stored, `index=True` Selection
# field whose entire stated purpose - per its own help text - is to let an operator
# tell "this route was MEASURED and the answer was 0 km" apart from "this route was
# NEVER MEASURED". `test_distance_status.py` proves the field computes correctly.
# THIS file proves an operator can ever SEE what it computed. Today they cannot:
# `distance_status` appears on zero views while its sibling `distance` appears on
# four - the field exists, is maintained, and is invisible. The only consumer today
# is the server log, which is not where an operator looks after seeing "0 km" on a
# form.
#
# ## Why the oracle is `get_view()` + arch parsing, not a grep of the .xml source
#
# `ir.ui.view.get_view()` is what the server actually serves to a client - it walks
# the inheritance chain (base view + every applicable `inherit_id` extension) and
# returns the RESOLVED `arch`. Asserting against that resolved arch is asserting the
# observable contract: it is what a real client renders, it survives future view
# inheritance from other modules, and it would catch a future module xpath-removing
# the field just as readily as it catches the field never having been added. A grep
# of the .xml source only proves text exists in a file - it says nothing about
# whether the server would ever serve that text to a client.


@tagged('post_install', '-at_install')
class TestDistanceStatusViewExposure(TransactionCase):
    """`distance_status` must be operator-visible everywhere `distance` is - the two
    fields describe the same reading and answering "was this measured" is only
    possible for a user who can see both.

    Six required exposures, resolved by XML id via `self.env.ref(...)` and read back
    through `get_view()` so the assertion is against the arch the server actually
    serves:

    1-3. `route.section.transport.mode` form / list / search - this is where the
         field is STORED and `index=True`; the search view additionally needs a
         group-by (see `test_transport_mode_search_view_exposes_group_by_on_distance_status`
         docstring for why).
    4-5. `route.section` form / list - the `store=False` mirror, so a user reading a
         section (not drilling into its transport-mode rows) has the same visibility.
    6.   `route.section` form AGAIN, this time inside the embedded
         `route.section.transport.mode` list nested under `transport_mode_ids` in the
         "Transportation Modes" notebook page - the single most operator-relevant
         surface, because it is where a user compares every provider's distance
         side by side for one section.

    ## Deliberately NOT asserted (do not "complete" this set later)

    - `to_geo_routes.view_route_section_search` (the `route.section` search view):
      the `route.section.distance_status` mirror is `store=False` with no search
      method (`route.section`'s field definition), so adding it to a search view would raise
      at view-validation time, not merely be a missing nicety. Its absence there is
      correct, not a gap.
    - Any `route.route` or `route.segment` view: neither model declares the field at
      all. `route.section.mixin` explains why - `route.segment` inherits the
      mixin but has no writer for `distance_status`, and "a status nothing ever sets
      is precisely the drift the design warns is worse than having no status".
      Asserting the field's presence there would demand a field that must not exist.
    """

    def _get_arch(self, view_xmlid, view_type):
        """Resolve `view_xmlid` and return its RESOLVED arch as an lxml Element -
        the inheritance-applied tree the server actually hands a client, not the
        raw .xml source text."""
        view = self.env.ref(view_xmlid)
        result = self.env[view.model].get_view(view.id, view_type)
        return etree.fromstring(result['arch'])

    def test_transport_mode_form_view_exposes_distance_status(self):
        """An operator opening one provider's route-section-mode form sees the
        distance the provider reported, but nothing on that form tells them whether
        a 0 km reading means "measured as zero" or "never measured" - they would
        have to go read the server log to find out."""
        arch = self._get_arch(
            'to_geo_routes.view_route_section_transport_mode_form', 'form')
        self.assertTrue(
            arch.findall(".//field[@name='distance_status']"),
            "distance_status is absent from the transport-mode form: an operator "
            "reading this provider's distance has no way, on this screen, to tell "
            "whether a 0 km reading was measured or never attempted.")

    def test_transport_mode_list_view_exposes_distance_status(self):
        """Scanning the transport-mode list (e.g. from the API log drill-down), an
        operator sees a column of distances with no column explaining which zeros
        are real measurements and which are unmeasured rows."""
        arch = self._get_arch(
            'to_geo_routes.view_route_section_transport_mode_list', 'list')
        self.assertTrue(
            arch.findall(".//field[@name='distance_status']"),
            "distance_status is absent from the transport-mode list: an operator "
            "scanning this list cannot distinguish a genuinely zero-distance row "
            "from a row that was never measured, even though the column is right "
            "there for `distance`.")

    def test_transport_mode_search_view_exposes_distance_status_as_searchable(self):
        """An operator cannot even filter on distance_status - "show me only the
        rows where measurement failed" is not answerable through the UI, forcing a
        developer console lookup for a question a search filter should answer."""
        arch = self._get_arch(
            'to_geo_routes.view_route_section_transport_mode_search', 'search')
        self.assertTrue(
            arch.findall(".//field[@name='distance_status']"),
            "distance_status is absent from the transport-mode search view: an "
            "operator cannot filter route-section-mode rows by measurement status "
            "at all, e.g. to find every row where the provider never returned a "
            "distance.")

    def test_transport_mode_search_view_offers_group_by_on_distance_status(self):
        """`distance_status` is declared `index=True` in
        `route.section.transport.mode` - an index earns its keep
        only if an operator can actually filter/group by the column it indexes.
        Without a group-by filter, "show me every section that could not be
        measured" - the operational payoff `index=True` was for - has no UI path,
        so the index protects a query nobody can issue."""
        arch = self._get_arch(
            'to_geo_routes.view_route_section_transport_mode_search', 'search')
        group_by_filters = [
            group_by_filter for group_by_filter in arch.findall(".//filter")
            if "'group_by': 'distance_status'" in (group_by_filter.get('context') or '')
        ]
        self.assertTrue(
            group_by_filters,
            "the transport-mode search view offers no group-by on distance_status: "
            "an operator cannot group route-section-mode rows by measurement "
            "outcome (e.g. to see how many sections were never measured), even "
            "though the field is indexed specifically to make that query cheap.")

    def test_section_form_view_exposes_distance_status(self):
        """An operator reading one route section's own form (not drilling into a
        specific provider's row) sees the section's distance with no indication of
        whether that reading was ever taken."""
        arch = self._get_arch('to_geo_routes.route_section_form', 'form')
        top_level_hits = [
            field_node for field_node in arch.findall(".//field[@name='distance_status']")
            if arch.find(".//field[@name='transport_mode_ids']") is None
            or field_node not in arch.find(".//field[@name='transport_mode_ids']").iter()
        ]
        self.assertTrue(
            top_level_hits,
            "distance_status is absent from the route.section form's own fields "
            "(outside the embedded transport-mode list): an operator reading a "
            "section's summary distance has no way to tell whether it was measured "
            "or never attempted.")

    def test_section_list_view_exposes_distance_status(self):
        """Scanning the route-section list, an operator sees a distance column with
        no companion column explaining which rows were genuinely measured at
        zero."""
        arch = self._get_arch('to_geo_routes.route_section_tree', 'list')
        self.assertTrue(
            arch.findall(".//field[@name='distance_status']"),
            "distance_status is absent from the route.section list: an operator "
            "scanning route sections cannot tell measured-zero rows from "
            "never-measured rows without opening each one.")

    def test_section_form_embedded_transport_mode_list_exposes_distance_status(self):
        """The embedded `route.section.transport.mode` list nested under
        `transport_mode_ids` in the "Transportation Modes" notebook page is the
        single most operator-relevant surface for this field: it is where a user
        compares every provider's distance for one section side by side. Without
        `distance_status` there, comparing providers by distance is comparing
        numbers that might not mean what they look like they mean - a 0 km
        provider row could be the cheapest route or a row that was never
        measured, and this list is exactly where that comparison happens."""
        arch = self._get_arch('to_geo_routes.route_section_form', 'form')
        transport_mode_ids_field = arch.find(".//field[@name='transport_mode_ids']")
        self.assertIsNotNone(
            transport_mode_ids_field,
            "the route.section form no longer embeds transport_mode_ids - the "
            "'Transportation Modes' notebook page this test targets is missing "
            "entirely.")
        self.assertTrue(
            transport_mode_ids_field.findall(".//field[@name='distance_status']"),
            "distance_status is absent from the embedded transport-mode list on "
            "the route.section form: an operator comparing providers side by side "
            "in the 'Transportation Modes' tab sees every provider's distance but "
            "cannot tell which zeros are real measurements, defeating the exact "
            "comparison this list exists for.")
