import ast
import io
import json
import re
import datetime
import logging
import tokenize
import unicodedata

from collections import defaultdict
from decimal import Decimal
from lxml import etree
from psycopg2 import sql

from odoo import api, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# tiny precompiled regex for minor perf gains
_ASCII_NONALNUM_RE = re.compile(r'[^0-9A-Za-z]+')
_MULTI_USCORE_RE = re.compile(r'_+')


class BaseModel(models.AbstractModel):
    """
    Allow to define a model with 'bigint' ID by setting the '_big_id' attribute to True.
    All the relational fields which relate to this model will be of type 'bigint' too.
    You can also manually set an integer field to 'bigint' by adding 'bigint=True' to it.
    """
    _inherit = 'base'
    _big_id = False

    def _auto_init(self):
        cr = self.env.cr
        columns_to_convert = []

        if self._big_id and 'id' in self._fields:
            self._fields['id'].column_type = ('bigint', 'bigint')
            # Because id is a special column and only create via first database initialization
            # so it won't convert into bigint serial unless we take action here
            columns_to_convert.append((self._table, 'id'))

        for field in self._fields.values():
            if not field.store:
                continue
            if field.type == 'many2one':
                if self.env[field.comodel_name]._big_id:
                    field.column_type = ('bigint', 'bigint')
            elif field.type == 'integer':
                if getattr(field, 'bigint', None):
                    field.column_type = ('bigint', 'bigint')
            elif field.type == 'many2many':
                if self._big_id:
                    columns_to_convert.append((field.relation, field.column1))
                if self.env[field.comodel_name]._big_id:
                    columns_to_convert.append((field.relation, field.column2))

        super(BaseModel, self)._auto_init()

        for table, column in columns_to_convert:
            cr.execute(
                "SELECT data_type FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                (table, column),
            )
            res = cr.fetchone()
            if res and res[0] != 'bigint':
                cr.execute(
                    sql.SQL("ALTER TABLE {table} ALTER COLUMN {column} TYPE bigint").format(
                        table=sql.Identifier(table),
                        column=sql.Identifier(column),
                    )
                )

    def _get_sort_key(self, record, custom_order=None, custom_order_position='before'):
        """
        Generate a dynamic sort key for a record based on a combination of the model's `_order` and a custom order.

        :param record: A single record.
        :param custom_order: A custom order string (e.g., 'field1 asc, field2 desc'). Defaults to None.
        :param custom_order_position: Determines the position of `custom_order` relative to the model's `_order`.
                                       - 'before': `custom_order` precedes `_order`.
                                       - 'after': `custom_order` follows `_order`.
                                       - 'replace': Only `custom_order` is used, replacing `_order`.
                                       Defaults to 'before'.
        :return: A tuple representing the sort key.
        """

        def normalize_value_for_sorting(value, direction):
            """
            Normalize a value for sorting based on direction ('asc' or 'desc').

            :param value: The value to normalize.
            :param direction: 'asc' or 'desc'.
            :return: Normalized value.
            """
            if value is None:
                return float('inf') if direction == 'asc' else -float('inf')

            if direction == 'desc':
                if isinstance(value, (int, float)):
                    return -value
                if isinstance(value, str):
                    return ''.join(chr(255 - ord(char)) for char in value)
                if isinstance(value, bool):
                    return not value
                if isinstance(value, (datetime.date, datetime.datetime)):
                    return -value.timestamp()
            return value

        model = record._name
        base_order = self.env[model]._order or 'id asc'

        # Combine orders based on the specified position
        if custom_order:
            if custom_order_position == 'before':
                combined_order = ', '.join(filter(None, [custom_order, base_order]))
            elif custom_order_position == 'after':
                combined_order = ', '.join(filter(None, [base_order, custom_order]))
            elif custom_order_position == 'replace':
                combined_order = custom_order
            else:
                raise ValidationError(
                    _(
                        "Invalid `custom_order_position` value: %s. Must be 'before', 'after', or 'replace'.",
                        custom_order_position
                    )
                )
        else:
            combined_order = base_order

        # Parse the combined order string
        order_fields = [
            (field.split()[0].strip(), 'desc' if 'desc' in field.lower() else 'asc')
            for field in combined_order.split(',')
        ]

        # Generate the sort key
        sort_values = []
        for field, direction in order_fields:
            value = getattr(record, field, None)
            sort_values.append(normalize_value_for_sorting(value, direction))
        return tuple(sort_values)

    def _group_by_company(self):
        """
        Group records in `self` by their `company_id`.

        This is useful when processing multi-company logic in batches,
        allowing operations to be applied per company context.

        Records without a `company_id` field (e.g., models that are not multi-company aware)
        will be grouped under the key `False`.

        Returns:
            dict: A dictionary where keys are `res.company` records (or False),
                  and values are recordsets (self-filtered by company).

        Example:
            >>> self.env['sale.order'].search([])._group_by_company()
            {
                res.company(1): sale.order(1, 2, 3),
                res.company(2): sale.order(4, 5),
                False: sale.order(6)
            }
        """
        grp = defaultdict(lambda: self.env[self._name])
        has_company = 'company_id' in self._fields
        for r in self:
            company = r.company_id if has_company else False
            grp[company] |= r
        return grp

    @api.model
    def _json_safe_value(self, value):
        """
        Convert a Python object into a JSON-safe representation.

        This method is used internally by `serialize(as_json_safe=True)` to ensure all field values
        can be safely passed to `json.dumps()` without raising serialization errors.

        Supported conversions:
            - datetime/date → ISO 8601 string (e.g., "2025-08-03T10:15:00")
            - Decimal → float
            - bytes → UTF-8 decoded string (or str fallback)
            - Odoo recordset (single) → ID
            - Other types → str(value) as fallback

        Args:
            value (any): The field value to be serialized.

        Returns:
            any: A JSON-serializable representation of the input value.
        """
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bytes):
            try:
                return value.decode()
            except Exception:
                return str(value)
        if hasattr(value, '_name') and hasattr(value, 'id'):  # record
            return value.id
        return value

    def serialize(
        self,
        depth=2,
        exclude_fields=None,
        exclude_field_types=None,
        as_json_safe=False,
        _current_path=None,
    ):
        """
        Serialize each record in ``self`` into a JSON-friendly list of dictionaries.

        The ``exclude_fields`` parameter supports the following formats:

        1. Global list or set, applied to every model at every level
           (backward compatible), for example::

               ['field_x', 'field_y']

        2. Per-model mapping (model name -> iterable of field names), for example::

               {
                   'res.partner': {'field_x', 'field_y'},
                   'sale.order': {'note'},
               }

        3. Per-scope mapping that uses a pair of ``(model_name, relation_path)``
           as key, for example::

               {
                   ('res.partner', ()): {'vat'},  # root partners
                   ('res.partner', ('child_ids',)): {'email', 'phone'},  # direct children
                   ('res.partner', ('child_ids', 'child_ids')): {'name'},  # grandchildren
               }

        These mapping styles can be combined in a single ``exclude_fields`` value.
        The effective set of excluded fields for a record is the union of:

        - the global excludes
        - the per-model excludes for the record model
        - the per-scope excludes for the pair ``(model_name, path)``
        """

        def normalize_exclude(raw_exclude):
            # Already normalized
            if isinstance(raw_exclude, dict) and {
                "_global",
                "_by_model",
                "_by_scope",
            }.issubset(raw_exclude.keys()):
                return {
                    "_global": set(raw_exclude["_global"]),
                    "_by_model": {
                        model_name: set(field_names)
                        for model_name, field_names in raw_exclude["_by_model"].items()
                    },
                    "_by_scope": {
                        (model_name, tuple(path)): set(field_names)
                        for (model_name, path), field_names in raw_exclude["_by_scope"].items()
                    },
                }

            normalized = {
                "_global": set(),
                "_by_model": {},
                "_by_scope": {},
            }

            if isinstance(raw_exclude, dict):
                for key, value in raw_exclude.items():
                    field_names = set(value or [])
                    if isinstance(key, tuple) and len(key) == 2:
                        model_name, path = key
                        scope_key = (model_name, tuple(path))
                        normalized["_by_scope"][scope_key] = (
                            normalized["_by_scope"].get(scope_key, set()) | field_names
                        )
                    elif isinstance(key, str):
                        normalized["_by_model"][key] = (
                            normalized["_by_model"].get(key, set()) | field_names
                        )
            else:
                normalized["_global"] = set(raw_exclude or [])

            return normalized

        if exclude_field_types is None:
            exclude_field_types = ["binary"]

        if _current_path is None:
            _current_path = ()

        normalized_exclude = normalize_exclude(exclude_fields or [])

        global_excludes = normalized_exclude["_global"]
        excludes_by_model = normalized_exclude["_by_model"]
        excludes_by_scope = normalized_exclude["_by_scope"]

        exclude_field_types_set = set(exclude_field_types)

        records_data = []
        for record in self:
            scope_key = (record._name, _current_path)

            record_excludes = set(global_excludes)
            record_excludes |= excludes_by_model.get(record._name, set())
            record_excludes |= excludes_by_scope.get(scope_key, set())

            record_data = {
                "id": record.id,
                "display_name": record.display_name,
            }

            for field_name, field in record._fields.items():
                if field_name in record_excludes:
                    continue
                if field.type in exclude_field_types_set:
                    continue

                try:
                    value = record[field_name]
                    if field.type == "many2one":
                        record_data[field_name] = value.id if value else False

                    elif field.type in ("one2many", "many2many"):
                        if depth > 0:
                            child_path = _current_path + (field_name,)
                            child_data = value.serialize(
                                depth=depth - 1,
                                exclude_fields=normalized_exclude,
                                exclude_field_types=exclude_field_types_set,
                                as_json_safe=as_json_safe,
                                _current_path=child_path,
                            )
                            record_data[field_name] = child_data
                        else:
                            record_data[field_name] = []

                    else:
                        record_data[field_name] = (
                            self._json_safe_value(value) if as_json_safe else value
                        )
                except Exception as e:
                    record_data[field_name] = False if field.type not in ("one2many", "many2many") else []
                    _logger.error(
                        f"Could not serialize the field {field_name} of the record {record.display_name} ({record._name}) due to error: {str(e)}"
                    )
                    continue

            records_data.append(record_data)

        return records_data

    @api.model
    def get_visible_fields_from_views(self, view_types=('form', 'tree')):
        """
        Return the set of field names that are effectively visible in any primary
        view of the given ``view_types``.

        Visibility rules:

        - Include ``<field name="...">`` if it is not permanently invisible.
        - Include "shadow-visible" fields referenced via widget options, for example
          the daterange ``end_date_field``, even if those fields are marked
          ``invisible="1"``.
        """

        def _parse_json_like(text):
            """Parse JSON or Python-literal dict safely; return {} on failure."""
            if not text:
                return {}
            # Try strict JSON first
            try:
                return json.loads(text)
            except Exception:
                pass
            # Try Python literal (single quotes, True/False, etc.)
            try:
                return ast.literal_eval(text)
            except Exception:
                return {}

        def _parse_modifiers(node):
            """Extract modifiers dict from 'modifiers' attribute if present."""
            mods_raw = node.attrib.get('modifiers')
            mods = _parse_json_like(mods_raw)
            return mods if isinstance(mods, dict) else {}

        def _is_hard_invisible_value(val):
            """
            Determine if a 'modifiers.invisible' value means permanently invisible.
            - True / 1 / "1" / "true" => permanent
            - list/tuple (domain) => conditional, NOT permanent
            """
            if isinstance(val, bool):
                return val
            if isinstance(val, (int,)):
                return bool(val)
            if isinstance(val, str):
                return val.strip().lower() in ('1', 'true')
            # Domain-like (list/tuple) => not permanent
            return False

        def is_permanently_invisible(field_el):
            """
            Determine if a field is permanently invisible (hard-coded) in OWL-based view XML.
            We check both legacy attributes and 'modifiers'.
            """
            invisible_attr = field_el.attrib.get('invisible', '').strip().lower()
            column_invisible_attr = field_el.attrib.get('column_invisible', '').strip().lower()
            if invisible_attr in ('1', 'true') or column_invisible_attr in ('1', 'true'):
                return True

            mods = _parse_modifiers(field_el)
            inv_mod = mods.get('invisible')
            col_inv_mod = mods.get('column_invisible')

            if _is_hard_invisible_value(inv_mod) or _is_hard_invisible_value(col_inv_mod):
                return True
            return False

        def _parse_options(node):
            """Get `options` as dict if present; empty dict otherwise."""
            opts_raw = node.attrib.get('options')
            opts = _parse_json_like(opts_raw)
            return opts if isinstance(opts, dict) else {}

        # Known widget option keys that reference other field(s)
        FIELD_OPTION_WHITELIST_BY_WIDGET = {
            # Your concrete case:
            'daterange': {'end_date_field', 'start_date_field'},

            # Common patterns worth honoring:
            'monetary': {'currency_field'},  # display format depends on a currency field
            'many2many_tags': {'color_field'},  # tag color often comes from another field
            'phone': {'country_field'},  # formatting/validation may use a country field
            'progressbar': {'max_field', 'value_field'},  # progress against another field
        }
        # Generic safety net: any option key that ends in these suffixes is *likely* a field reference
        GENERIC_FIELD_KEY_SUFFIXES = ('_field', '_fields')

        visible = set()

        # Use sudo to avoid ACL issues while reading view definitions
        views = self.env['ir.ui.view'].sudo().search([
            ('model', '=', self._name),
            ('type', 'in', view_types),
            ('mode', '=', 'primary'),
        ])

        for view in views:
            try:
                arch_xml = view.get_combined_arch()
                if not arch_xml or not arch_xml.strip().startswith('<'):
                    _logger.debug("Skipping view %s - invalid XML arch", view.id)
                    continue

                root = etree.fromstring(arch_xml)

                for field_el in root.xpath('.//field[@name]'):
                    field_name = field_el.attrib.get('name')
                    if not field_name:
                        continue

                    # 1) Direct visibility
                    if not is_permanently_invisible(field_el):
                        visible.add(field_name)

                    # 2) Shadow visibility via widget options
                    widget = (field_el.attrib.get('widget') or '').strip().lower()
                    options = _parse_options(field_el)

                    # Determine which option keys to inspect
                    keys_to_check = set()
                    if widget in FIELD_OPTION_WHITELIST_BY_WIDGET:
                        keys_to_check |= FIELD_OPTION_WHITELIST_BY_WIDGET[widget]
                    for key in options.keys():
                        for suffix in GENERIC_FIELD_KEY_SUFFIXES:
                            if key.endswith(suffix):
                                keys_to_check.add(key)
                                break

                    # Pull field names from those option keys
                    for key in keys_to_check:
                        opt_val = options.get(key)
                        # Single field reference
                        if isinstance(opt_val, str):
                            if opt_val in self._fields:
                                visible.add(opt_val)
                        # Multiple fields reference
                        elif isinstance(opt_val, (list, tuple)):
                            for maybe_name in opt_val:
                                if isinstance(maybe_name, str) and maybe_name in self._fields:
                                    visible.add(maybe_name)
                        # Ignore other types

            except Exception as e:
                _logger.warning("Failed to parse view %s (%s): %s", view.id, view.type, e)

        return visible

    @api.model
    def get_invisible_fields_from_views(self, view_types=('form', 'tree')):
        """
        Get a list of fields defined on the model but not visible in any views of the given types.

        This is computed as: all model fields - visible fields in views.

        Args:
            view_types (tuple): A tuple of view types to inspect (e.g., ('form', 'tree'))

        Returns:
            list[str]: Field names that are not used in the specified view types
        """
        visible_fields = self.get_visible_fields_from_views(view_types=view_types)
        return list(set(self._fields) - visible_fields)

    # ----------------------------------------
    # Code generation & normalization
    # ----------------------------------------
    @api.model
    def _normalize_str(self, text, *, uppercase=False):
        """Normalize a string into an ASCII-safe slug used for codes/identifiers.

        Steps:
          - strip accents using NFKD
          - replace non-alphanumeric chars with '_'
          - collapse consecutive '_' and trim leading/trailing '_'
          - optionally uppercase the result

        Args:
            text (str | None): Any input text.
            uppercase (bool): If True, return the slug uppercased.

        Returns:
            str: Normalized slug (e.g., 'SALES_ORDER', 'abc_123'); empty string if input is falsy.
        """
        s = (text or '').strip()
        if not s:
            return ''
        # Accent strip to ASCII
        normalized = unicodedata.normalize('NFKD', s)
        ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
        # Non-alnum -> underscore
        ascii_text = _ASCII_NONALNUM_RE.sub('_', ascii_text)
        # Collapse and trim underscores
        ascii_text = _MULTI_USCORE_RE.sub('_', ascii_text).strip('_')
        if uppercase:
            ascii_text = ascii_text.upper()
        return ascii_text

    def _find_unique_code(self, text, code_field_name=False):
        """Derive a company-scoped unique code from a label; append the smallest free numeric suffix if needed.

        Behavior:
          - Base code = _normalize_str(text, uppercase=True)
          - Search (case-insensitive) for existing values starting with the base within the same company
            (or shared records where company_id is False, if the model has company_id)
          - If base is taken, append the smallest free suffix: _2, _3, ...

        Args:
            text (str): Source label to derive the code from.
            code_field_name (str | bool): Target field name to check/store the code; defaults to 'code'.

        Returns:
            str: Unique code (e.g., 'INVENTORY', 'INVENTORY_2').

        Raises:
            ValidationError: If the model has no such field, or if a valid base code cannot be derived.
        """
        field_name = code_field_name or 'code'
        if field_name not in self._fields:
            raise ValidationError(_("The model `%(name)s` has no field named `%(field_name)s`", name=self._name, field_name=field_name))
        self.ensure_one()

        base_code = self._normalize_str(text, uppercase=True)
        if not base_code:
            raise ValidationError(_("Cannot derive code from empty/invalid text."))

        # Company filter if applicable: allow shared records (False) and the current company
        company_domain = []
        if 'company_id' in self._fields:
            company_domain = [('company_id', 'in', [self.company_id.id, False])]

        # Exclude current records from the check (when renaming/updating)
        domain = [(field_name, 'ilike', f'{base_code}%')] + company_domain
        if self.ids:
            domain.append(('id', 'not in', list(self.ids)))

        # Fetch only the needed field for performance
        rows = self.with_context(active_test=False).search_read(domain, fields=[field_name])
        existing_codes = {r.get(field_name) for r in rows if r.get(field_name)}

        # Base code is free
        if base_code not in existing_codes:
            return base_code

        # Find the smallest available numeric suffix starting at 2
        pat = re.compile(rf'^{re.escape(base_code)}_(\d+)$')
        taken = set()
        for c in existing_codes:
            m = pat.match(c)
            if m:
                try:
                    taken.add(int(m.group(1)))
                except Exception:
                    # Ignore non-integer tails
                    pass
        i = 2
        while i in taken:
            i += 1
        return f'{base_code}_{i}'

    @api.model
    def _convert_vals_to_xml_id(self, vals, module_name, prefix=None):
        """Build a stable external XML ID from incoming values and model context.

        Rules:
          - If the model has `company_id`, `vals['company_id']` is required and included in the XML ID.
          - Prefer `vals['code']` when present and non-empty on models that have a 'code' field; else require `vals['name']`.
          - Model segment and value segment are normalized via `_normalize_str()` and lowercased.

        Args:
            vals (dict): Values used to create/identify the record (must include required keys).
            module_name (str): Technical module name to prefix the XML ID.
            prefix (str | None): Optional override for the model segment; defaults to `self._name`.

        Returns:
            str: XML ID in one of the forms:
                 '{module}.{company_id}_{normalized_model}_{normalized_value}'  (if model has company_id)
                 '{module}.{normalized_model}_{normalized_value}'               (otherwise)
                 e.g., 'viin_sale.3_res_company_viindoo', 'viin_core.res_partner_acme_inc'.

        Raises:
            ValidationError: If required keys are missing or normalization results in an empty value.
        """
        # Company part (only if the model actually has company_id)
        if 'company_id' in self._fields:
            if 'company_id' not in vals:
                raise ValidationError(_("`company_id` was not in the given vals to create xml_id!"))
            company_part = f"{vals['company_id']}_"
        else:
            company_part = ''

        # Choose the identifier key: prefer non-empty 'code' when available; else require 'name'
        id_key = None
        if 'code' in self._fields and vals.get('code'):
            id_key = 'code'
        elif 'name' in vals and vals.get('name'):
            id_key = 'name'
        else:
            # Tell user what is missing, depending on whether 'code' is an available field
            missing = 'code or name' if 'code' in self._fields else 'name'
            raise ValidationError(_("`%s` was not in the given vals to create xml_id!", missing))

        normalized_model = self._normalize_str(prefix or self._name).lower()
        normalized_value = self._normalize_str(vals[id_key]).lower()
        if not normalized_value:
            raise ValidationError(_("Normalized XML ID value is empty."))

        return f"{module_name}.{company_part}{normalized_model}_{normalized_value}"

    @api.model
    def _strip_python_comments(self, src):
        """Remove Python comments safely (keeps strings intact)."""
        if not (src or '').strip():
            return ''
        try:
            rl = io.StringIO(src).readline
            tokens = []
            for tok in tokenize.generate_tokens(rl):
                tok_type, _tok_val, _start, _end, _line = tok
                if tok_type == tokenize.COMMENT:
                    # drop comments entirely
                    continue
                tokens.append(tok)
            # Rebuild code from tokens (preserves spacing/newlines)
            expr = tokenize.untokenize(tokens)
        except Exception:
            # Fallback: simple line-based strip for robustness
            lines = []
            for ln in (src or '').splitlines():
                # keep line if not starting with '#'
                if not ln.lstrip().startswith('#'):
                    # also drop pure-comment suffix if line becomes empty
                    # (avoid naive split on '#' to not break strings)
                    lines.append(ln)
            expr = '\n'.join(lines)

        return expr.strip()
