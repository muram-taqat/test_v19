from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPermissiveRuleShortCircuit(TransactionCase):
    """Guards ir.rule._get_rules (to_base): when a user has a *permissive* group
    rule ([(1,'=',1)] or empty domain) for a model, the redundant (e.g. deep
    relational) group rules are dropped so they never get compiled into SQL -
    WITHOUT changing effective access. Uses res.partner.category (no record
    rules by default) so only the rules created here apply.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.model = 'res.partner.category'
        cls.Rule = cls.env['ir.rule']
        cls.Category = cls.env['res.partner.category']
        cls.model_id = cls.env['ir.model']._get_id(cls.model)
        Groups = cls.env['res.groups']
        cls.grp_perm = Groups.create({'name': 'ZZ Permissive group'})
        cls.grp_deep = Groups.create({'name': 'ZZ Deep group'})
        internal = cls.env.ref('base.group_user')
        # a restrictive ("deep") group rule: only the SECRET category is allowed
        cls.deep_rule = cls.Rule.create({
            'name': 'ZZ deep rule',
            'model_id': cls.model_id,
            'groups': [(6, 0, cls.grp_deep.ids)],
            'domain_force': "[('name', '=', 'ZZ_SECRET')]",
        })
        cls.user_both = cls.env['res.users'].create({
            'name': 'zz both', 'login': 'zz_both',
            'group_ids': [(6, 0, (internal | cls.grp_perm | cls.grp_deep).ids)],
        })
        cls.user_deep = cls.env['res.users'].create({
            'name': 'zz deep only', 'login': 'zz_deep',
            'group_ids': [(6, 0, (internal | cls.grp_deep).ids)],
        })
        cls.cat_public = cls.Category.create({'name': 'ZZ_PUBLIC'})
        cls.cat_secret = cls.Category.create({'name': 'ZZ_SECRET'})

    def _rules(self, user):
        return self.Rule.with_user(user)._get_rules(self.model, 'read')

    def _add_perm_rule(self, domain="[(1, '=', 1)]"):
        return self.Rule.create({
            'name': 'ZZ permissive rule',
            'model_id': self.model_id,
            'groups': [(6, 0, self.grp_perm.ids)],
            'domain_force': domain,
        })

    # --- rule-set filtering behaviour ---------------------------------------

    def test_permissive_drops_redundant_deep_rule(self):
        # THE core behaviour: a permissive group rule makes the deep one redundant.
        perm = self._add_perm_rule()
        rules = self._rules(self.user_both)
        self.assertIn(perm, rules, "permissive rule is kept")
        self.assertNotIn(self.deep_rule, rules, "redundant deep group rule is dropped")

    def test_empty_domain_counts_as_permissive(self):
        # empty domain_force -> normalize_domain([]) -> [TRUE_LEAF] -> match all
        perm = self._add_perm_rule(domain=False)
        rules = self._rules(self.user_both)
        self.assertIn(perm, rules)
        self.assertNotIn(self.deep_rule, rules)

    def test_without_permissive_keeps_deep_rule(self):
        # a user with no permissive rule is untouched: the deep rule stays.
        rules = self._rules(self.user_deep)
        self.assertIn(self.deep_rule, rules)

    def test_global_rule_always_kept(self):
        glob = self.Rule.create({
            'name': 'ZZ global rule', 'model_id': self.model_id,
            'groups': False, 'domain_force': "[('name', '!=', False)]",
        })
        self._add_perm_rule()
        rules = self._rules(self.user_both)
        self.assertIn(glob, rules, "global (AND) rules are never dropped")
        self.assertNotIn(self.deep_rule, rules)

    # --- query-count guard ---------------------------------------------------

    def test_get_rules_query_budget(self):
        # Cold-cache budget, measured on this branch. The absolute number is
        # core's to move - it was 4 on 18.0 and is 10 on 19.0, because 19.0
        # resolves the acting user's groups and the model's rules through more
        # statements before this override ever runs. What the guard actually
        # pins is the LAST of those statements: the ir_rule row fetch must be
        # ONE fetch covering the WHOLE rule set, global rules included, which is
        # what with_prefetch(rules_su._ids) buys.
        # Dropping that with_prefetch call narrows _prefetch_ids to the group
        # rules, splits that fetch in two and costs exactly one query more (11
        # here, 5 on 18.0) - so the guard keeps its teeth at the measured value.
        # That is the regression which breaks core's query-count perf tests
        # (test_mail_full TestRatingPerformance: 1615 > 1614 on runbot).
        # Re-measure (do not just bump) whenever core changes _get_rules: run
        # this test with the budget set to 0 and read the reported count, then
        # confirm removing with_prefetch still costs +1.
        self._add_perm_rule()
        glob = self.Rule.create({
            'name': 'ZZ global budget rule', 'model_id': self.model_id,
            'groups': False, 'domain_force': "[('name', '!=', 'ZZ_NOPE')]",
        })
        self.env.flush_all()
        self.env.registry.clear_all_caches()
        self.env.invalidate_all()
        Rule = self.Rule.with_user(self.user_both)
        with self.assertQueryCount(10):
            rules = Rule._get_rules(self.model, 'read')
            # everything _compute_domain reads next must already be in cache
            rules_su = rules.sudo()
            rules_su.mapped('domain_force')
            rules_su.mapped('groups')
        self.assertIn(glob, rules, "budget test must cover a global rule")

    # --- effective access is unchanged (the correctness guard) --------------

    def test_access_unchanged_for_permissive_user(self):
        # With a permissive rule, the user sees records the deep rule would
        # EXCLUDE - exactly as core does (deep OR permissive == TRUE). The
        # short-circuit must not accidentally restrict them.
        self._add_perm_rule()
        visible = self.Category.with_user(self.user_both).search(
            [('id', 'in', (self.cat_public | self.cat_secret).ids)])
        self.assertIn(self.cat_public, visible)
        self.assertIn(self.cat_secret, visible)

    def test_restricted_user_still_restricted(self):
        # A user WITHOUT a permissive rule must remain restricted by the deep
        # rule: it must not be broadened by the short-circuit.
        visible = self.Category.with_user(self.user_deep).search(
            [('id', 'in', (self.cat_public | self.cat_secret).ids)])
        self.assertNotIn(self.cat_public, visible, "deep rule still hides non-matching records")
        self.assertIn(self.cat_secret, visible)
