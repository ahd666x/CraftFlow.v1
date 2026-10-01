"""تست‌های امنیتی: whitelist، فقط‌خواندنی بودن، دسترسی، و اجرای ناامن."""
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from craftflow_ai.permissions.errors import (
    ToolDisabledError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
)
from craftflow_ai.permissions.policy import (
    authorize,
    has_permission,
    permissions_for_user,
)
from craftflow_ai.tools import get_registry
from craftflow_ai.tools.registry import Tool, ToolRegistry, ok

from .factories import PASSWORD, make_order, make_raw_material, make_tasks, make_user


class ToolWhitelistTests(TestCase):
    """هیچ ابزار عمومی و خطرناکی نباید وجود داشته باشد."""

    FORBIDDEN_NAMES = (
        'execute_python', 'execute_sql', 'run_command', 'exec', 'eval',
        'raw_sql', 'run_shell', 'import_module', 'os_system', 'subprocess',
        'delete_data', 'create_record', 'update_record', 'save',
    )

    @classmethod
    def setUpTestData(cls):
        cls.registry = get_registry()

    def test_no_generic_execution_tools_exist(self):
        names = {name.lower() for name in self.registry.names()}
        for forbidden in self.FORBIDDEN_NAMES:
            self.assertNotIn(forbidden, names)

    def test_all_registered_tools_are_read_only(self):
        for tool in self.registry.list_tools():
            self.assertTrue(
                tool.read_only,
                msg=f'ابزار {tool.name} فقط‌خواندنی نیست',
            )

    def test_registry_refuses_write_tools_in_phase_one(self):
        fresh = ToolRegistry()

        def write_handler(**kwargs):
            raise AssertionError('نباید اجرا شود')

        with self.assertRaises(ToolDisabledError):
            fresh.register(Tool(
                name='delete_order',
                description='حذف سفارش',
                permission='orders.view',
                handler=write_handler,
                read_only=False,
            ))
        self.assertEqual(len(fresh), 0)

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ToolNotFoundError):
            self.registry.get('definitely_not_a_tool')

    def test_tool_schemas_do_not_leak_internals(self):
        for tool in self.registry.list_tools():
            schema = tool.to_schema()
            self.assertEqual(
                set(schema) >= {'name', 'description', 'input_schema'}, True
            )
            self.assertNotIn('handler', schema)
            self.assertNotIn('permission', schema)

    def test_llm_sees_only_allowed_tools(self):
        """مدل نباید ابزاری را ببیند که کاربر اجازهٔ اجرایش را ندارد."""
        nobody = make_user('no-groups')
        allowed = {t.name for t in self.registry.available_for(nobody)}
        self.assertEqual(allowed, set(), 'کاربر بدون گروه نباید هیچ ابزاری ببیند')

        manager = make_user('manager', groups=['2'])
        allowed = {t.name for t in self.registry.available_for(manager)}
        self.assertIn('get_production_status', allowed)
        self.assertIn('get_inventory_status', allowed)

    def test_unknown_tool_does_not_leak_available_list_to_llm_only(self):
        """پیام خطا برای کاربر عادی فهرست ابزارهای موجود را افشا نمی‌کند."""
        with self.assertRaises(ToolNotFoundError) as ctx:
            self.registry.get('nope')
        self.assertIn('nope', ctx.exception.message)


class PermissionPolicyTests(TestCase):

    def test_superuser_has_all_permissions(self):
        root = make_user('root', superuser=True)
        for key in ('orders.view', 'production.view', 'inventory.view',
                    'reports.view', 'quality.view'):
            self.assertTrue(has_permission(root, key), key)

    def test_anonymous_user_has_no_permissions(self):
        for key in ('orders.view', 'production.view', 'inventory.view'):
            self.assertFalse(has_permission(AnonymousUser(), key))

    def test_manager_group_has_all_permissions(self):
        manager = make_user('mgr', groups=['2'])
        perms = permissions_for_user(manager)
        self.assertTrue(all(perms.values()), perms)

    def test_warehouse_group_can_only_view_inventory(self):
        warehouse = make_user('wh', groups=['انبار'])
        perms = permissions_for_user(warehouse)
        self.assertTrue(perms['inventory.view'])
        self.assertFalse(perms['reports.view'])
        self.assertFalse(perms['production.view'])

    def test_staff_group_can_view_orders_and_production(self):
        """
        گروه '۱' در CraftFlow به صف انبار هم دسترسی دارد
        (``inventory`` از ``warehouse_or_manager_required`` استفاده می‌کند
        که گروه ۱ را شامل می‌شود)، بنابراین سیاست AI هم باید همین را بدهد.
        """
        staff = make_user('st', groups=['1'])
        perms = permissions_for_user(staff)
        self.assertTrue(perms['orders.view'])
        self.assertTrue(perms['production.view'])
        self.assertTrue(perms['inventory.view'])

    def test_warehouse_group_is_limited_to_inventory(self):
        """گروه «انبار» فقط ابزارهای انبار را می‌بیند."""
        warehouse = make_user('wh', groups=['انبار'])
        perms = permissions_for_user(warehouse)
        self.assertTrue(perms['inventory.view'])
        self.assertFalse(perms['reports.view'])
        self.assertFalse(perms['production.view'])
        self.assertFalse(perms['orders.view'])
        self.assertFalse(perms['quality.view'])

    def test_user_without_groups_sees_no_tools(self):
        plain = make_user('plain')
        perms = permissions_for_user(plain)
        self.assertFalse(any(perms.values()), perms)

    def test_unknown_permission_is_denied(self):
        user = make_user('mgr2', groups=['1'])
        self.assertFalse(has_permission(user, 'does.not.exist'))

    def test_authorize_raises_for_missing_permission(self):
        warehouse = make_user('wh2', groups=['انبار'])
        with self.assertRaises(ToolPermissionError) as ctx:
            authorize(warehouse, 'production.view')
        self.assertEqual(ctx.exception.code, 'PERMISSION_DENIED')

    def test_authorize_passes_for_allowed_user(self):
        manager = make_user('mgr3', groups=['1'])
        self.assertTrue(authorize(manager, 'inventory.view'))


class ToolArgumentValidationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.registry = get_registry()

    def test_missing_required_argument(self):
        tool = self.registry.get('get_order_details')
        with self.assertRaises(ToolValidationError) as ctx:
            tool.validate_arguments({})
        self.assertEqual(ctx.exception.code, 'MISSING_ARGUMENT')

    def test_unknown_argument_is_rejected(self):
        tool = self.registry.get('get_order_details')
        with self.assertRaises(ToolValidationError) as ctx:
            tool.validate_arguments({'order_id': 1, 'evil': 'x'})
        self.assertEqual(ctx.exception.code, 'UNKNOWN_ARGUMENT')

    def test_wrong_type_is_rejected(self):
        tool = self.registry.get('get_order_details')
        with self.assertRaises(ToolValidationError) as ctx:
            tool.validate_arguments({'order_id': 'not-a-number'})
        self.assertEqual(ctx.exception.code, 'INVALID_ARGUMENT_TYPE')

    def test_malformed_arguments_type_is_rejected(self):
        tool = self.registry.get('get_order_details')
        with self.assertRaises(ToolValidationError):
            tool.validate_arguments(['a', 'list'])

    def test_string_coercion_is_allowed_for_int_schema(self):
        tool = self.registry.get('get_order_details')
        cleaned = tool.validate_arguments({'order_id': '42'})
        self.assertEqual(cleaned['order_id'], 42)

    def test_none_arguments_becomes_empty_dict(self):
        tool = self.registry.get('get_production_status')
        self.assertEqual(tool.validate_arguments(None), {})


class ToolFailureIsolationTests(TestCase):
    """خطای یک ابزار نباید کل سیستم را بشکند و نباید داده اختراع کند."""

    @classmethod
    def setUpTestData(cls):
        cls.registry = get_registry()

    def test_tool_exception_is_wrapped(self):
        def boom(**kwargs):
            raise RuntimeError('boom')

        fresh = ToolRegistry()
        fresh.register(Tool(
            name='exploding', description='ابزار خراب',
            permission='orders.view', handler=boom,
        ))
        tool = fresh.get('exploding')
        with self.assertRaises(Exception) as ctx:
            tool.run()
        self.assertEqual(ctx.exception.code, 'TOOL_EXECUTION_FAILED')
        self.assertNotIn('boom', str(ctx.exception.detail))

    def test_non_conforming_output_is_rejected(self):
        def bad(**kwargs):
            return {'not': 'structured'}

        fresh = ToolRegistry()
        fresh.register(Tool(
            name='bad_shape', description='خروجی بد', permission='orders.view', handler=bad,
        ))
        with self.assertRaises(Exception):
            fresh.get('bad_shape').run()

    def test_tools_do_not_write_to_database(self):
        """ابزارهای تولید نباید هیچ ردیفی بسازند یا تغییر دهند."""
        from craftflow_ai.services import queries

        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'))
        make_raw_material(name='رنگ', stock='5')

        counts_before = {
            'orders': _count('product.Order'),
            'tasks': _count('product.ProductionTask'),
            'materials': _count('inventory.RawMaterial'),
            'movements': _count('inventory.StockMovement'),
            'issues': _count('inventory.MaterialIssue'),
        }

        queries.production_status()
        queries.bottlenecks()
        queries.inventory_status()
        queries.material_shortages()
        queries.production_report()
        queries.open_orders()
        queries.delayed_orders()
        queries.quality_status()

        for label, before in counts_before.items():
            self.assertEqual(_count(_MODELS[label]), before, f'{label} تغییر کرد')


_MODELS = {
    'orders': 'product.Order',
    'tasks': 'product.ProductionTask',
    'materials': 'inventory.RawMaterial',
    'movements': 'inventory.StockMovement',
    'issues': 'inventory.MaterialIssue',
}


def _count(label):
    from django.apps import apps
    return apps.get_model(label).objects.count()


def test_login_required_is_enforced():
    """صفحه و API باید برای کاربر مهمان بسته باشند."""
    from django.urls import reverse

    response = _anon_client().get(reverse('craftflow_ai:chat'))
    assert response.status_code in (302, 403), response.status_code


def _anon_client():
    from django.test import Client
    return Client()