from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    config_parameters = env['ir.config_parameter'].search(
        [('key', '=', 'report.print_delay'), ('value', '=', '10000')])
    config_parameters.write({'value': '3000'})
