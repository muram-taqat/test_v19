from psycopg2.errors import CheckViolation

from odoo import Command
from odoo.tools import mute_logger
from odoo.tests import tagged, Form
from odoo.exceptions import ValidationError, UserError

from .common import TestCommon


@tagged('post_install', '-at_install')
class TestRoutes(TestCommon):

    def test_route_compute_with_waypoints_01(self):
        """ Test compute departure, destination, route section lines with input:
            - Create a new route with 3 waypoints HN->HP->QN
            - No section line exists
        """
        self.assertRecordValues(self.route, [{
            'address_from_id': self.address_hn.id,
            'address_to_id': self.address_qn.id
        }])
        self.assertRecordValues(self.route.segment_ids, [{
            'address_from_id': self.address_hn.id,
            'address_to_id': self.address_hp.id,
            'ave_speed': 60.00000000000001,
        }, {
            'address_from_id': self.address_hp.id,
            'address_to_id': self.address_qn.id,
            'ave_speed': 60.00000000000001,
        }])
        segment_1, segment_2 = self.route.segment_ids
        # distance/duration are derived from a haversine (trig-based) computation,
        # which is subject to libm/glibc ULP drift across platforms - compare with
        # a tight-but-tolerant precision instead of exact float equality.
        self.assertAlmostEqual(segment_1.distance, 93.41196317221592, places=6)
        self.assertAlmostEqual(segment_1.duration, 1.5568660528702651, places=6)
        self.assertAlmostEqual(segment_2.distance, 43.33259242342841, places=6)
        self.assertAlmostEqual(segment_2.duration, 0.7222098737238067, places=6)

    def test_route_compute_with_waypoints_02(self):
        """ Test compute total_distance, ave_speed, duration, route section lines with input:
            - Create a new route with 3 waypoints HN->HP->QN
            - Already exists route section that matches your waypoint
        """
        self.route.unlink()
        existing_route_section = self.env['route.section'].search(
            ['|', '&', ('address_from_id', '=', self.address_hn.id), ('address_to_id', '=', self.address_hp.id),
             '&', ('address_from_id', '=', self.address_hp.id), ('address_to_id', '=', self.address_qn.id)])
        self.assertEqual(len(existing_route_section), 2)

        route = self.env['route.route']
        with Form(route) as f:
            f.name = 'Form Test'
            f.transport_mode = 'driving'
            with f.waypoint_ids.new() as line:
                line.address_id = self.address_hn
            with f.waypoint_ids.new() as line:
                line.address_id = self.address_hp
            with f.waypoint_ids.new() as line:
                line.address_id = self.address_qn
            route = f.save()
        # Expected values
        self.assertRecordValues(route, [{
            'ave_speed': 60.00000000000001,
        }])
        # distance/duration are derived from a haversine (trig-based) computation,
        # which is subject to libm/glibc ULP drift across platforms - compare with
        # a tight-but-tolerant precision instead of exact float equality.
        self.assertAlmostEqual(route.distance, 136.74455559564433, places=6)
        self.assertAlmostEqual(route.duration, 2.279075926594072, places=6)
        self.assertRecordValues(route.segment_ids, [{
            'address_from_id': self.address_hn.id,
            'address_to_id': self.address_hp.id,
            'ave_speed': 60.00000000000001,
        }, {
            'address_from_id': self.address_hp.id,
            'address_to_id': self.address_qn.id,
            'ave_speed': 60.00000000000001,
        }])
        segment_1, segment_2 = route.segment_ids
        self.assertAlmostEqual(segment_1.distance, 93.41196317221592, places=6)
        self.assertAlmostEqual(segment_1.duration, 1.5568660528702651, places=6)
        self.assertAlmostEqual(segment_2.distance, 43.33259242342841, places=6)
        self.assertAlmostEqual(segment_2.duration, 0.7222098737238067, places=6)

    def test_compute_departure_dest_when_changing_sequence(self):
        route = self.route
        self.waypoint_hn.sequence = 10
        self.waypoint_qn.sequence = 20

        route.write({
            'waypoint_ids': [(6, 0, (self.waypoint_hn | self.waypoint_qn).ids)]
        })
        self.assertEqual(route.address_from_id, self.address_hn)
        self.assertEqual(route.address_to_id, self.address_qn)

        with self.env.cr.savepoint():
            self.waypoint_hn.sequence = 20
            self.waypoint_qn.sequence = 10
        self.assertRecordValues(route, [{
            'address_from_id': self.address_qn.id,
            'address_to_id': self.address_hn.id
        }])

    def test_route_direction(self):
        """ Test route same waypoints adjacent to each other. Ex: B->B->...
            Expected result: raise
        """
        with self.assertRaises(ValidationError):
            self.env['route.route'].create({
                'name': 'Route test',
                'transport_mode': 'driving',
                'waypoint_ids': [(0, 0, {
                    'address_id': self.address_hn.id
                }), (0, 0, {
                    'address_id': self.address_hn.id
                })]
            })

    def test_create_same_route(self):
        # The route this one has to collide with is built here rather than taken
        # from demo data: Odoo 19 loads no demo by default, so neither the demo
        # round trip (route_r00004) nor base.res_partner_1 exists on a default
        # install. The A->B->A shape is what route_r00004 had.
        def _round_trip_vals(name):
            return {
                'name': name,
                'transport_mode': 'driving',
                'waypoint_ids': [
                    Command.create({
                        'address_id': self.address_hn.id,
                        'sequence': 10,
                        }),
                    Command.create({
                        'address_id': self.address_qn.id,
                        'sequence': 20,
                        }),
                    Command.create({
                        'address_id': self.address_hn.id,
                        'sequence': 30,
                        }),
                    ]
                }

        self.env['route.route'].create(_round_trip_vals('Ha Noi -> Quang Ninh -> Ha Noi'))
        # now create a new route with the same data
        with self.assertRaises(UserError):
            self.env['route.route'].create(
                _round_trip_vals('Route that is the same as Ha Noi -> Quang Ninh -> Ha Noi')
                )

    def test_negative_values_route_section(self):
        route_section = self.env['route.section'].search([('address_from_id', '=', self.address_hn.id)], limit=1)
        with mute_logger('odoo.sql_db'), self.assertRaises(CheckViolation, msg="Distance should not be negative."):
            route_section.transport_mode_ids.distance = -1
        with mute_logger('odoo.sql_db'), self.assertRaises(CheckViolation, msg="Distance in miles should not be negative."):
            route_section.transport_mode_ids.distance_in_miles = -1
        with mute_logger('odoo.sql_db'), self.assertRaises(CheckViolation, msg="Speed should not be negative."):
            route_section.transport_mode_ids.ave_speed = -40
        with mute_logger('odoo.sql_db'), self.assertRaises(CheckViolation, msg="Speed in mph should not be negative."):
            route_section.transport_mode_ids.ave_speed_in_miles = -40

    def test_waypoint_area_restricted_none(self):
        self.address_hn.waypoint_area_id = self.waypoint_area.id
        self.address_qn.waypoint_area_id = self.waypoint_area.id

    def test_waypoint_area_restricted_by_state(self):
        self.waypoint_area.restricted_by = 'state'
        self.assertNotEqual(self.address_hn.state_id, self.address_qn.state_id)
        self.address_hn.waypoint_area_id = self.waypoint_area.id
        with self.assertRaises(UserError):
            self.address_qn.waypoint_area_id = self.waypoint_area.id

    def test_waypoint_area_restricted_by_country(self):
        self.waypoint_area.restricted_by = 'state'
        self.address_hn.country_id = self.env.ref('base.be').id
        self.assertNotEqual(self.address_hn.country_id, self.address_qn.country_id)
        self.address_hn.waypoint_area_id = self.waypoint_area.id
        with self.assertRaises(UserError):
            self.address_qn.waypoint_area_id = self.waypoint_area.id

    def test_mapping_address_with_nameless_waypoint_partner(self):
        """ mapping_address must not crash when a waypoint address partner has no name.

            An address-type contact (e.g. an "Other Address" child) legitimately has an
            empty ``name`` - only its ``display_name`` is set. The mapping must fall back
            to that display_name instead of joining a ``False`` value into the string.
        """
        parent = self.env['res.partner'].create({'name': 'Delizia Coop'})
        nameless_address = self.env['res.partner'].create({
            'name': False,
            'type': 'other',
            'parent_id': parent.id,
            'city': 'Zoppola',
        })
        self.assertFalse(nameless_address.name)
        self.assertTrue(nameless_address.display_name)

        route = self.env['route.route'].create({
            'name': 'Nameless waypoint route',
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({'address_id': nameless_address.id}),
                Command.create({'address_id': self.address_hp.id}),
            ],
        })

        mapping = route.mapping_address
        self.assertIsInstance(mapping, str)
        self.assertIn(nameless_address.display_name, mapping)
        self.assertIn(self.address_hp.name, mapping)
