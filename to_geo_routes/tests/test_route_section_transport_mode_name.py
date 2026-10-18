from odoo.tests import tagged, TransactionCase


@tagged('post_install', '-at_install')
class TestRouteSectionTransportModeName(TransactionCase):

    def test_compute_name_without_provider(self):
        """Compute name must not crash on a record whose provider is not yet
        set - e.g. a fresh record being initialized in a form view before the
        transport mode provider is chosen. The name must still resolve to a
        usable string instead of raising.
        """
        section_mode = self.env['route.section.transport.mode'].new({})
        name = section_mode.name
        self.assertTrue(isinstance(name, str) and name)
