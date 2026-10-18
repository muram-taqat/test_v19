import re

from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError


class WaypointArea (models.Model):
    _name = 'route.waypoint.area'
    _description = "Waypoint Area"
    _parent_name = 'parent_id'
    _parent_store = True
    _rec_name = 'complete_name'
    _order = 'parent_path'

    name = fields.Char(string='Waypoint Area', translate=True, required=True)
    restricted_by = fields.Selection([
        ('none', 'Non-Restricted'),
        ('state', 'State'),
        ('country', 'Country')], string='Restricted By', required=True, default='none',
        help='Partner Addresses linked to this area will be restricted by either state or country or non.\n'
        'For example, when this are is set restricted by state of Hanoi, only partners with addresses outside Hanoi '
        'cannot be linked to this area.'
        )
    partner_ids = fields.One2many(
        'res.partner', 'waypoint_area_id',
        string='Partners',
        help='The partners that is listed in this area'
        )
    parent_id = fields.Many2one(
        'route.waypoint.area', string='Parent Area', ondelete='restrict', index=True)
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('route.waypoint.area', 'parent_id', string='Child Areas')
    complete_name = fields.Char(
        string='Complete Name', compute='_compute_complete_name', recursive=True,
        search='_search_complete_name')

    @api.depends('name', 'parent_id.complete_name')
    @api.depends_context('lang')
    def _compute_complete_name(self):
        for r in self:
            if r.parent_id:
                r.complete_name = '%s / %s' % (r.parent_id.complete_name, r.name)
            else:
                r.complete_name = r.name

    def _search_complete_name(self, operator, value):
        supported_operators = ['=', '!=', 'ilike', 'not ilike', 'in', 'not in', '=ilike']
        if operator not in supported_operators or not isinstance(value, (str, list)):
            return NotImplemented
        area = self.env['route.waypoint.area'].search([])
        if operator == '=':
            area = area.filtered(lambda m: m.complete_name == value)
        elif operator == '!=':
            area = area.filtered(lambda m: m.complete_name != value)
        elif operator == 'ilike':
            area = area.filtered(lambda m: value.lower() in (m.complete_name or '').lower())
        elif operator == 'not ilike':
            area = area.filtered(lambda m: value.lower() not in (m.complete_name or '').lower())
        elif operator == 'in':
            area = area.filtered(lambda m: m.complete_name in value)
        elif operator == 'not in':
            area = area.filtered(lambda m: m.complete_name not in value)
        elif operator == '=ilike':
            pattern = re.compile(re.escape(value).replace('%', '.*').replace('_', '.'), flags=re.IGNORECASE)
            area = area.filtered(lambda m: pattern.fullmatch(m.complete_name or ''))
        return [('id', 'in', area.ids)]

    @api.model
    def name_create(self, name):
        area = self.create({'name': name})
        return area.id, area.display_name

    @api.constrains('restricted_by', 'partner_ids')
    def _check_restricted_by(self):
        for r in self:
            if r.restricted_by == 'state':
                if len(r.partner_ids.state_id) > 1:
                    raise UserError(
                        _('The current partners linked to this waypoint area have different states.\n'
                          ' Hence, you can not make this waypoint area to restrict partners by state')
                        )
            elif r.restricted_by == 'country':
                if len(r.partner_ids.country_id) > 1:
                    raise UserError(
                        _('The current partners linked to this waypoint area are in different countries.\n'
                          ' Hence, you can not make this waypoint area to restrict partners by country')
                        )

    @api.constrains('parent_id')
    def _check_area_recursion(self):
        if self._has_cycle():
            raise ValidationError(_('You cannot create recursive waypoint areas.'))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_has_child_areas(self):
        if self.child_ids:
            raise UserError(_('You cannot delete a waypoint area that still has child areas. '
                              'Delete or move its child areas first.'))
