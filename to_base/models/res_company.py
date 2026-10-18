from odoo import fields, models


class Company(models.Model):
    _inherit = 'res.company'

    font = fields.Selection(selection_add=[('Times New Roman', 'Times New Roman')])
