from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSerializeScope(TransactionCase):
    """
    Scope-aware serialize() tests.

    These tests assume serialize() supports:
      - Per-model excludes: {"res.partner": {"field_x", ...}}
      - Per-scope excludes: {("model.name", ("path", ...)): {"field_y", ...}}
    And that nested one2many/many2many calls propagate a logical path so that
    self-relational models (res.partner/child_ids) and other relations can be
    filtered independently.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.User = cls.env["res.users"]
        cls.Channel = cls.env["discuss.channel"]
        cls.ChannelMember = cls.env["discuss.channel.member"]

        # Root partner
        cls.root_partner = cls.Partner.create({
            "name": "Root Partner",
            "email": "root@example.com",
            "street": "Root Street",
        })

        # Child partner (self one2many via parent_id)
        cls.child_partner = cls.Partner.create({
            "name": "Child Partner",
            "email": "child@example.com",
            "street": "Child Street",
            "parent_id": cls.root_partner.id,
        })

        # Ensure relation visible through child_ids
        cls.root_partner.child_ids |= cls.child_partner

        # User linked to root partner via partner_id (backed by user_ids o2m)
        cls.linked_user = cls.User.create({
            "name": "Linked User",
            "login": "linked.user@example.com",
            "email": "linked.user@example.com",
            "partner_id": cls.root_partner.id,
        })

        # Channel linked via partner.channel_ids
        cls.channel = cls.Channel.create({
            "name": "Test Channel",
        })

        # v18: create membership via discuss.channel.member
        cls.ChannelMember.create({
            "partner_id": cls.root_partner.id,
            "channel_id": cls.channel.id,
            "new_message_separator": 0,
        })

    def test_child_ids_scope_exclusion(self):
        """
        Root res.partner and its children under child_ids must see different
        effective exclusions based on (model, path).
        """
        exclude_fields = {
            # Per-model exclude for all res.partner records
            "res.partner": {"street"},
            # Root scope: hide email
            ("res.partner", ()): {"email"},
            # Children under child_ids: hide name instead
            ("res.partner", ("child_ids",)): {"name"},
        }

        serialized_list = self.root_partner.serialize(
            depth=2,
            exclude_fields=exclude_fields,
            as_json_safe=True,
        )
        self.assertEqual(len(serialized_list), 1)
        root_data = serialized_list[0]

        # Root scope exclusions
        self.assertNotIn("street", root_data)
        self.assertNotIn("email", root_data)
        self.assertEqual(root_data["id"], self.root_partner.id)
        self.assertIn("display_name", root_data)

        # Child scope under child_ids
        self.assertIn("child_ids", root_data)
        self.assertEqual(len(root_data["child_ids"]), 1)
        child_data = root_data["child_ids"][0]

        # Model-level exclude
        self.assertNotIn("street", child_data)
        # Scope-level for children
        self.assertNotIn("name", child_data)
        # Email is not excluded in child scope here
        self.assertIn("email", child_data)

    def test_user_ids_scope_exclusion(self):
        """
        Users reached via partner.user_ids must be filtered by the
        (res.users, ('user_ids',)) scope without affecting other users.
        """
        if "user_ids" not in self.Partner._fields:
            self.skipTest("user_ids not defined on res.partner in this environment")

        exclude_fields = {
            # Exclude sensitive fields for all res.users
            "res.users": {"password", "api_key_ids", "apikey_ids"},
            # For users under user_ids from partner:
            # drop name/email/partner_id leaving only id/login and other non-excluded fields
            ("res.users", ("user_ids",)): {
                "name",
                "email",
                "partner_id",
            },
        }

        serialized_list = self.root_partner.serialize(
            depth=2,
            exclude_fields=exclude_fields,
            as_json_safe=True,
        )
        root_data = serialized_list[0]

        self.assertIn("user_ids", root_data)
        self.assertGreaterEqual(len(root_data["user_ids"]), 1)

        for user_data in root_data["user_ids"]:
            # Global exclusions
            self.assertNotIn("password", user_data)
            self.assertNotIn("api_key_ids", user_data)
            self.assertNotIn("apikey_ids", user_data)

            # Scope-specific
            self.assertIn("id", user_data)
            self.assertIn("login", user_data)
            self.assertNotIn("name", user_data)
            self.assertNotIn("email", user_data)
            self.assertNotIn("partner_id", user_data)

    def test_channel_ids_scope_exclusion(self):
        """
        Channels under channel_ids must respect their own scope without
        impacting other channels.
        """
        if "channel_ids" not in self.Partner._fields:
            self.skipTest("channel_ids not defined on res.partner in this environment")

        exclude_fields = {
            # For channels reached via partner.channel_ids, hide some technical fields
            ("discuss.channel", ("channel_ids",)): {
                "channel_partner_ids",
                "channel_last_seen_partner_ids",
                "uuid",
                "description",
            },
        }

        serialized_list = self.root_partner.serialize(
            depth=2,
            exclude_fields=exclude_fields,
            as_json_safe=True,
        )
        root_data = serialized_list[0]

        self.assertIn("channel_ids", root_data)
        self.assertGreaterEqual(len(root_data["channel_ids"]), 1)

        for channel_data in root_data["channel_ids"]:
            self.assertIn("id", channel_data)
            self.assertIn("name", channel_data)

            self.assertNotIn("channel_partner_ids", channel_data)
            self.assertNotIn("channel_last_seen_partner_ids", channel_data)
            self.assertNotIn("uuid", channel_data)
            self.assertNotIn("description", channel_data)
