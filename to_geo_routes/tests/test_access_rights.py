from odoo.fields import Command
from odoo.tests import tagged
from odoo.exceptions import AccessError

from .common import TestCommon


@tagged('post_install', '-at_install', 'access_rights')
class TestRoutes(TestCommon):

    @classmethod
    def setUpClass(cls):
        super(TestRoutes, cls).setUpClass()
        # Build dedicated users instead of restricting the shared demo and admin
        # records. Odoo 19 forbids stripping base.group_system from the last active
        # administrator, and base.user_root is archived, so overwriting base.user_admin
        # leaves the system group with no visible member and aborts setUpClass.
        # group_user implies base.group_user and group_admin implies group_user, so a
        # single group per user reproduces exactly what the overwrite used to grant.
        cls.user_demo = cls.env['res.users'].create({
            'name': 'Geo Route User',
            'login': 'geo_route_user',
            'email': 'geo.route.user@example.viindoo.com',
            'group_ids': [Command.set([cls.env.ref('to_geo_routes.group_user').id])],
        })
        cls.user_geo_admin = cls.env['res.users'].create({
            'name': 'Geo Route Administrator',
            'login': 'geo_route_admin',
            'email': 'geo.route.admin@example.viindoo.com',
            'group_ids': [Command.set([cls.env.ref('to_geo_routes.group_admin').id])],
        })

    # GEO USER
    def test_geo_user_access_route(self):
        route = self.env['route.route'].with_user(self.user_demo).create({
            'name': 'Route test',
            'transport_mode': 'driving',
        })
        route.name = 'Route Test -'

        with self.assertRaises(AccessError):
            route.unlink()

    def test_geo_user_access_waypoint(self):
        route = self.env['route.route'].with_user(self.user_demo).create({
            'name': 'Route test',
            'transport_mode': 'driving',
        })
        waypoint = self.env['route.waypoint'].with_user(self.user_demo).create({
            'address_id': self.address_hn.id,
            'route_id': route.id
        })
        waypoint.read(['address_id'])
        waypoint.address_id = self.address_qn.id,
        waypoint.unlink()

    def test_geo_user_access_route_section(self):
        route_section = self.env['route.section'].with_user(self.user_demo).create({
            'address_from_id': self.address_hp.id,
            'address_to_id': self.address_hn.id,
            'distance': 100,
            'ave_speed': 75
        })
        route_section.read(['address_from_id'])
        route_section.distance = 1000
        route_section.unlink()

    def test_geo_user_access_route_segment(self):
        route_segment = self.env['route.segment'].with_user(self.user_demo).create({
            'route_id': self.route.id,
            'address_from_id': self.address_hn.id,
            'address_to_id': self.address_qn.id,
        })
        route_segment.read(['address_to_id'])
        route_segment.address_from_id = self.env['res.partner'].create({'name': 'just for test'})
        try:
            route_segment.unlink()
        except Exception:
            self.fail("%s should be able to delete the section")

    def test_geo_user_access_waypoint_area(self):
        waypoint_area = self.env['route.waypoint.area'].with_user(self.user_demo).create({
            'name': 'Area test',
            'restricted_by': 'state'
        })
        waypoint_area.restricted_by = 'country'
        waypoint_area.unlink()

    # GEO ADMIN

    def test_geo_admin_access_route(self):
        self.assertIn(self.env.ref('to_geo_routes.group_user'), self.env.ref('to_geo_routes.group_admin').implied_ids)
        route = self.env['route.route'].with_user(self.user_geo_admin).create({
            'name': 'Route test',
            'transport_mode': 'driving',
        })
        route.unlink()
