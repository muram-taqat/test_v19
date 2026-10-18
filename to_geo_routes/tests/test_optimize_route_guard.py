from odoo import Command
from odoo.tests import tagged, TransactionCase


@tagged('post_install', '-at_install')
class TestOptimizeRouteGuard(TransactionCase):
    """Regression for acceptance scenarios S17/S19.

    `action_optimize_route` (route.mixin._optimize_route -> res.partner._get_optimized_route)
    must be a graceful no-op for a route with fewer than 2 waypoints - there is nothing to
    optimise. It must NOT crash with an unhandled IndexError (0 waypoints) nor raise the
    provider's "Origin and destination cannot be the same" error (1 waypoint).

    The 'driving' manual provider (data/transport_mode_provider_data.xml,
    manual_input_driving) is always loaded with the module, so a route with
    transport_mode='driving' always has a non-empty transport_mode_provider_ids without any
    extra fixture - this reaches the REAL crashing code path (no mocking required).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.address_a = cls.env['res.partner'].create({
            'name': 'Optimize Guard Address A',
            'partner_latitude': 21.010458,
            'partner_longitude': 105.787416,
        })

    def test_optimize_route_zero_waypoints_no_crash(self):
        """0 waypoints: res.partner._get_optimized_route would do self[0] on an empty
        recordset (IndexError) without the guard."""
        route = self.env['route.route'].create({
            'name': 'Zero waypoint route',
            'transport_mode': 'driving',
        })
        self.assertEqual(len(route.waypoint_ids), 0)
        self.assertTrue(
            route.transport_mode_provider_ids,
            "the 'manual' driving provider must be enabled by default data for this test to "
            "exercise the real optimize-route code path"
            )

        try:
            route.action_optimize_route()
        except Exception as e:  # noqa: BLE001 - intentionally broad: must NOT raise at all
            self.fail("action_optimize_route() must be a graceful no-op for 0 waypoints, "
                       "not raise: %r" % (e,))

        # graceful no-op: route left unchanged
        self.assertEqual(len(route.waypoint_ids), 0)

    def test_optimize_route_one_waypoint_no_crash(self):
        """1 waypoint: origin == destination and waypoints is empty, so the provider
        raises "Origin and destination cannot be the same...", which
        res.partner._get_optimized_route re-raises at the user without the guard."""
        route = self.env['route.route'].create({
            'name': 'One waypoint route',
            'transport_mode': 'driving',
            'waypoint_ids': [Command.create({'address_id': self.address_a.id})],
        })
        self.assertEqual(len(route.waypoint_ids), 1)
        self.assertTrue(
            route.transport_mode_provider_ids,
            "the 'manual' driving provider must be enabled by default data for this test to "
            "exercise the real optimize-route code path"
            )

        try:
            route.action_optimize_route()
        except Exception as e:  # noqa: BLE001 - intentionally broad: must NOT raise at all
            self.fail("action_optimize_route() must be a graceful no-op for 1 waypoint, "
                       "not raise: %r" % (e,))

        # graceful no-op: route left unchanged
        self.assertEqual(len(route.waypoint_ids), 1)
        self.assertEqual(route.waypoint_ids.address_id, self.address_a)

    def test_optimize_route_two_waypoints_still_raises_when_no_provider(self):
        """Guard must not swallow the pre-existing ValidationError for '>= 2 waypoints but
        no provider enabled' - only < 2 waypoints is a no-op.

        `transport_mode_provider_ids` is a non-stored computed field (always includes any
        active 'manual' provider matching the route's transport_mode), so to reach the
        "no provider" branch we archive the data-shipped manual provider for the 'water'
        mode instead of writing to the field directly.
        """
        from odoo.exceptions import ValidationError

        self.env.ref('to_geo_routes.manual_input_water').active = False

        address_b = self.env['res.partner'].create({
            'name': 'Optimize Guard Address B',
            'partner_latitude': 20.861135,
            'partner_longitude': 106.672549,
        })
        route = self.env['route.route'].create({
            'name': 'Two waypoint route, no provider',
            'transport_mode': 'water',  # keep 'driving' data provider out of this route
            'waypoint_ids': [
                Command.create({'address_id': self.address_a.id}),
                Command.create({'address_id': address_b.id}),
            ],
        })
        self.assertFalse(route.transport_mode_provider_ids)
        self.assertEqual(len(route.waypoint_ids), 2)

        with self.assertRaises(ValidationError):
            route.action_optimize_route()
