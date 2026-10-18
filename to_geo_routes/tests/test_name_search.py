from odoo.tests import tagged

from .common import TestCommon


@tagged('post_install', '-at_install')
class TestNameSearch(TestCommon):
    """Locks the name-search-by-extra-field behavior: a route must be findable by a
    substring of its `code`, and a route section must be findable by a substring of
    either endpoint address's `name` - not only by their own `name`/`display_name`.
    """

    def test_route_name_search_by_code(self):
        """A route.route must be found by a substring of its `code`, even though that
        substring never appears in the route's own `name` (so a default name-only
        match cannot accidentally make this pass)."""
        route = self.env['route.route'].create({
            'name': 'Confidential Cargo Lane',
            'transport_mode': 'driving',
            'code': 'ZQX-9981',
        })
        found_ids = [rec_id for rec_id, _display_name in self.env['route.route'].name_search('ZQX-9981')]
        self.assertIn(route.id, found_ids)
        # sanity: an unrelated route whose name/code do not contain the search term
        # must not show up - guards against a "match everything" regression too.
        self.assertNotIn(self.route.id, found_ids)

    def test_route_section_name_search_by_endpoint_address_name(self):
        """A route.section must be found by a substring of either endpoint address's
        `name`. Two sections are created with disjoint endpoint names so that a
        regression that stops scoping the match to the right addresses - whether by
        matching nothing or by matching every section - is caught either way."""
        partner_from_a = self.env['res.partner'].create({'name': 'Zylonport Terminal'})
        partner_to_a = self.env['res.partner'].create({'name': 'Nightshade Crossing'})
        partner_from_b = self.env['res.partner'].create({'name': 'Umberglade Wharf'})
        partner_to_b = self.env['res.partner'].create({'name': 'Foxglove Junction'})

        section_a = self.env['route.section'].create({
            'address_from_id': partner_from_a.id,
            'address_to_id': partner_to_a.id,
        })
        section_b = self.env['route.section'].create({
            'address_from_id': partner_from_b.id,
            'address_to_id': partner_to_b.id,
        })

        # match via address_from_id.name
        found_from = [rec_id for rec_id, _display_name in self.env['route.section'].name_search('Zylonport')]
        self.assertIn(section_a.id, found_from)
        self.assertNotIn(section_b.id, found_from)

        # match via address_to_id.name
        found_to = [rec_id for rec_id, _display_name in self.env['route.section'].name_search('Nightshade')]
        self.assertIn(section_a.id, found_to)
        self.assertNotIn(section_b.id, found_to)
