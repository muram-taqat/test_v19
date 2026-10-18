from odoo import Command
from odoo.tests import TransactionCase


class RouteCommonFunc(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    @classmethod
    def _create_simple_route(cls, company, code='RT-TEST-001'):
        """Create a minimal but valid route for testing."""
        partner_model = cls.env['res.partner']

        address_from = partner_model.create({
            'name': 'Route Test From',
            'company_id': company.id,
        })
        address_to = partner_model.create({
            'name': 'Route Test To',
            'company_id': company.id,
        })

        route_model = cls.env['route.route']
        route = route_model.create({
            'company_id': company.id,
            'code': code,
            # transport_mode is provided by transport.mode.mixin
            # We assume 'driving' is a valid option; adjust if needed.
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({
                    'address_id': address_from.id,
                    'sequence': 10,
                }),
                Command.create({
                    'address_id': address_to.id,
                    'sequence': 20,
                }),
            ],
        })
        return route
