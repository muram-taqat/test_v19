from odoo.tests import tagged
from odoo.exceptions import UserError

from .common import TestCommon


@tagged('post_install', '-at_install')
class TestWaypointAreaTree(TestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('vi_VN')

    def test_child_zone_reads_full_ancestor_path_via_complete_name(self):
        parent = self.env['route.waypoint.area'].create({'name': 'Asia'})
        child = self.env['route.waypoint.area'].create({
            'name': 'Vietnam',
            'parent_id': parent.id,
        })

        expected = 'Asia / Vietnam'
        self.assertEqual(child.complete_name, expected)
        self.assertEqual(child.display_name, expected)

    def test_renaming_grandparent_zone_updates_grandchild_complete_name(self):
        grandparent = self.env['route.waypoint.area'].create({'name': 'Asia'})
        parent = self.env['route.waypoint.area'].create({
            'name': 'Vietnam',
            'parent_id': grandparent.id,
        })
        child = self.env['route.waypoint.area'].create({
            'name': 'Hanoi',
            'parent_id': parent.id,
        })
        self.assertEqual(child.complete_name, 'Asia / Vietnam / Hanoi')

        grandparent.write({'name': 'APAC'})

        self.assertEqual(child.complete_name, 'APAC / Vietnam / Hanoi')

    def test_creating_parent_child_cycle_is_rejected(self):
        # `_parent_store = True` is what refuses the cycle, so this pins the contract
        # (refused, tree unchanged) and goes red if _parent_store is ever dropped. No
        # message is asserted: the wording is the ORM's.
        area_a = self.env['route.waypoint.area'].create({'name': 'Area A'})
        area_b = self.env['route.waypoint.area'].create({
            'name': 'Area B',
            'parent_id': area_a.id,
        })
        parent_before = area_a.parent_id

        with self.assertRaises(UserError), self.env.cr.savepoint():
            area_a.write({'parent_id': area_b.id})

        # assertRaises alone would pass even if the write had landed before raising.
        self.env.invalidate_all()
        self.assertEqual(area_a.parent_id, parent_before)
        self.assertEqual(area_b.parent_id, area_a)

    def test_zones_sharing_a_name_are_told_apart_by_their_path(self):
        parent_a = self.env['route.waypoint.area'].create({'name': 'Vietnam'})
        parent_b = self.env['route.waypoint.area'].create({'name': 'Cambodia'})

        child_a = self.env['route.waypoint.area'].create({
            'name': 'Hai Phong',
            'parent_id': parent_a.id,
        })
        child_b = self.env['route.waypoint.area'].create({
            'name': 'Hai Phong',
            'parent_id': parent_b.id,
        })

        self.assertEqual(child_a.name, child_b.name)
        self.assertNotEqual(child_a.complete_name, child_b.complete_name)

    def test_deleting_zone_with_children_is_blocked(self):
        parent = self.env['route.waypoint.area'].create({'name': 'Vietnam'})
        self.env['route.waypoint.area'].create({
            'name': 'Hanoi',
            'parent_id': parent.id,
        })

        with self.assertRaises(UserError):
            parent.unlink()
        self.assertTrue(parent.exists())

    def test_restricted_by_state_constraint_does_not_recurse_into_child_zone_partners(self):
        parent = self.env['route.waypoint.area'].create({
            'name': 'Vietnam',
            'restricted_by': 'state',
        })
        self.address_hn.waypoint_area_id = parent.id
        child = self.env['route.waypoint.area'].create({
            'name': 'Hai Phong',
            'parent_id': parent.id,
        })

        # address_hp is in a different state than address_hn (common.py); linking it to
        # the CHILD must not be weighed against the PARENT's state check.
        self.address_hp.waypoint_area_id = child.id

        # Both halves matter: the link really landed on the child, and the parent did
        # not absorb it - otherwise the test passes because nothing happened.
        self.assertEqual(child.partner_ids, self.address_hp)
        self.assertEqual(parent.partner_ids, self.address_hn)

    def test_name_search_requires_the_full_path_so_imports_cannot_guess(self):
        # Names are deliberately unlike anything in demo data: the assertions below are
        # about exact result SETS, so a zone created elsewhere with the same name would
        # break them.
        land = self.env['route.waypoint.area'].create({'name': 'Testland'})
        onia = self.env['route.waypoint.area'].create({'name': 'Testonia'})
        harbour_land = self.env['route.waypoint.area'].create({
            'name': 'Test Harbour', 'parent_id': land.id,
        })
        harbour_onia = self.env['route.waypoint.area'].create({
            'name': 'Test Harbour', 'parent_id': onia.id,
        })

        full_path_matches = self.env['route.waypoint.area'].name_search(
            harbour_land.complete_name, operator='=')
        self.assertEqual(
            {area_id for area_id, _label in full_path_matches},
            {harbour_land.id},
            "a full complete_name path must resolve to exactly the zone it names")

        # operator='=' is how an import resolves a many2one label; it keeps the first of several hits.
        self.assertFalse(
            self.env['route.waypoint.area'].name_search('Test Harbour', operator='='),
            "a bare zone name must not resolve on the import door - once siblings may "
            "share a name it no longer names one zone")

        # The picker is untouched: its default ilike still offers both, because the bare
        # name is a substring of every complete_name ending in it.
        self.assertEqual(
            {area_id for area_id, _label
             in self.env['route.waypoint.area'].name_search('Test Harbour')},
            {harbour_land.id, harbour_onia.id},
            "typing a bare name in the zone field must still offer both zones, told "
            "apart by their path")

    def test_complete_name_follows_the_readers_language_regardless_of_last_writer(self):
        parent = self.env['route.waypoint.area'].create({'name': 'Vietnam'})
        child = self.env['route.waypoint.area'].create({
            'name': 'Hai Phong',
            'parent_id': parent.id,
        })

        # vi_VN is written last: a stored (frozen) path would read back Vietnamese
        # regardless of which language later asks for it.
        parent.with_context(lang='vi_VN').write({'name': 'Viet Nam'})
        child.with_context(lang='vi_VN').write({'name': 'Hai Phong (VN)'})

        self.assertEqual(
            child.with_context(lang='vi_VN').complete_name, 'Viet Nam / Hai Phong (VN)')
        self.assertEqual(
            child.with_context(lang='en_US').complete_name, 'Vietnam / Hai Phong')

    def test_name_search_by_complete_name_matches_the_readers_language_full_path(self):
        parent = self.env['route.waypoint.area'].create({'name': 'Vietnam'})
        child = self.env['route.waypoint.area'].create({
            'name': 'Hai Phong',
            'parent_id': parent.id,
        })
        parent.with_context(lang='vi_VN').write({'name': 'Viet Nam'})
        child.with_context(lang='vi_VN').write({'name': 'Hai Phong (VN)'})

        # touch complete_name in another language first, so a match below proves the
        # search itself resolves by the reader's language, not by whichever language
        # happened to compute the path first.
        self.assertTrue(child.complete_name)

        matches = self.env['route.waypoint.area'].with_context(lang='vi_VN').name_search(
            'Viet Nam / Hai Phong (VN)', operator='=')

        self.assertEqual(
            {area_id for area_id, _label in matches}, {child.id},
            "the vi_VN full path (the import path for operator='=') must resolve to "
            "the zone by its vi_VN complete_name, not its en_US one")

    def test_siblings_are_listed_contiguous_with_their_parent_not_alphabetically(self):
        zebra = self.env['route.waypoint.area'].create({'name': 'Zebra'})
        alpha = self.env['route.waypoint.area'].create({
            'name': 'Alpha',
            'parent_id': zebra.id,
        })
        beta = self.env['route.waypoint.area'].create({'name': 'Beta'})

        # Names chosen so an alphabetical order would put Beta between Zebra and its child.
        ordered = self.env['route.waypoint.area'].search([
            ('id', 'in', [zebra.id, alpha.id, beta.id]),
        ]).ids

        self.assertEqual(ordered.index(alpha.id), ordered.index(zebra.id) + 1)

    def test_quick_create_files_the_typed_text_as_the_zone_name(self):
        area_id, _label = self.env['route.waypoint.area'].name_create('Test Quickland')

        area = self.env['route.waypoint.area'].browse(area_id)
        self.assertEqual(area.name, 'Test Quickland')
        self.assertFalse(area.parent_id)
