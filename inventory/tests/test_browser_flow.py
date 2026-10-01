"""Diagnostic: simulates real browser HTTP flow for the custody feature."""
import json
import re
from decimal import Decimal
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from django.contrib.auth.models import User
from django.test import LiveServerTestCase
from django.urls import reverse

from inventory.models import (
    MaterialCustody, MaterialIssue, MaterialLeftover, RawMaterial, RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color, Customer, Order, OrderItem, PaintingProcess, Product, ProductCategory,
    ProductionDefect,
)

LOGIN = '/accounts/login/'


class BrowserFlowTests(LiveServerTestCase):
    def setUp(self):
        self.wh = User.objects.create_superuser('wh', password='pw')
        self.worker = User.objects.create_user('ali', password='pw', first_name='Ali')
        self.cat = RawMaterialCategory.objects.create(name='Paint')
        self.raw = RawMaterial.objects.create(
            category=self.cat, name='Paint', unit='kg', pack_size=Decimal('4.50'),
        )
        pcat = ProductCategory.objects.create(name='C')
        product = Product.objects.create(category=pcat, name='P', base_price=1)
        customer = Customer.objects.create(name='M', phone='1')
        order = Order.objects.create(user=self.wh, customer=customer, number='B1')
        item = OrderItem.objects.create(order=order, product=product, quantity=1)
        Color.objects.create(part='body', code='8', orderitem=item)
        PaintingProcess.objects.create(name='P', code='B', color_codes=['8'], is_active=True)
        self.order, self.item = order, item
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('100'))

        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.get(LOGIN)
        self.opener.open(
            self.live_server_url + LOGIN,
            urlencode({
                'username': 'wh', 'password': 'pw',
                'csrfmiddlewaretoken': self.csrf(), 'next': '/',
            }).encode(),
        ).read()

    # -- helpers -------------------------------------------------------
    def csrf(self):
        for c in self.jar:
            if c.name == 'csrftoken':
                return c.value
        return ''

    def get(self, path):
        return self.opener.open(self.live_server_url + path).read().decode()

    def url(self, name, **kw):
        return self.live_server_url + reverse(name, **kw)

    def post_json(self, name, payload):
        req = Request(
            self.live_server_url + reverse(name),
            data=json.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'X-CSRFToken': self.csrf(),
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': self.live_server_url + reverse(name),
            },
            method='POST',
        )
        resp = self.opener.open(req)
        return resp.status, resp.read().decode()

    def make_issue(self, qty='3'):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.item, color_part='body',
            quantity=1, description='d', reported_by=self.wh,
        )
        return MaterialIssue.objects.create(
            defect=defect, raw_material=self.raw, requested_quantity=Decimal(qty),
            purpose='rework', status='requested', requested_by=self.wh,
        )

    # -- tests ---------------------------------------------------------
    def test_queue_page(self):
        html = self.get(reverse('inventory:production_issue_queue'))
        print('[1] queue page OK')
        print('    nav custody link present :', 'inventory/custody/' in html)
        print('    toggle in JS             :', 'handoverCustodyToggle' in html)
        print('    warning box present      :', 'custody_users' not in html)

    def test_dump_rendered_html(self):
        issue = self.make_issue('3')
        self.post_json('inventory:handover_create', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'received_by': self.worker.pk,
            'held_by': self.worker.pk,
        })
        html = self.get(reverse('inventory:custody_board'))

        print('\n===== CUSTODY BOARD: button markup =====')
        for m in re.findall(r'<button[^>]*btn-custody-return[^>]*>', html, re.S):
            print(m)

        print('\n===== CUSTODY BOARD: table tag =====')
        for m in re.findall(r'<table[^>]*>', html):
            print(m)

        print('\n===== CUSTODY BOARD: scripts =====')
        for m in re.findall(r'<script[^>]*src="([^"]+)"', html):
            print(' src:', m)

        print('\n===== CUSTODY BOARD: modal id present =====')
        print(' custodyReturnModal:', 'id="custodyReturnModal"' in html)
        print(' bootstrap global script:', 'bootstrap.bundle' in html)

        q = self.get(reverse('inventory:production_issue_queue'))
        print('\n===== QUEUE: toggle markup (rendered by JS, expect absent) =====')
        print(' has custodyToggle id in html:', 'id="handoverCustodyToggle"' in q)

    def test_full_flow(self):
        issue = self.make_issue('3')

        st, body = self.post_json('inventory:handover_preview', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'held_by': self.worker.pk,
        })
        print('[2] preview  status:', st)
        print('    body:', body[:220])

        st, body = self.post_json('inventory:handover_create', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'received_by': self.worker.pk,
            'held_by': self.worker.pk,
            'note': '',
        })
        print('[3] create   status:', st)
        print('    body:', body[:220])

        print('    MaterialCustody rows :', list(MaterialCustody.objects.values()))
        print('    MaterialLeftover rows:', list(MaterialLeftover.objects.values()))
        print('    raw.current_stock    :', self.raw.current_stock)

        html = self.get(reverse('inventory:custody_board'))
        print('[4] custody board rows:', re.findall(r'data-custody="(\d+)"', html))
        print('    contains worker name :', 'Ali' in html)

        st, body = self.post_json('inventory:custody_return', {
            'raw_material_id': self.raw.pk,
            'held_by': self.worker.pk,
            'measured_quantity': '2.5',
            'note': '',
        })
        print('[5] return   status:', st)
        print('    body:', body[:220])
        print('    custody after return:', list(MaterialCustody.objects.values()))

    def test_create_without_held_by(self):
        """Same as before but no custody flag -> legacy leftover path."""
        issue = self.make_issue('3')
        st, body = self.post_json('inventory:handover_create', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'received_by': self.worker.pk,
            'note': '',
        })
        print('[6] legacy create status:', st, body[:160])
        print('    custody:', list(MaterialCustody.objects.values()))
        print('    leftover:', list(MaterialLeftover.objects.values()))

    def test_dump_rendered_script(self):
        """Write the REAL rendered <script> to a file so it can be syntax-checked."""
        import pathlib
        import re as _re

        html = self.get(reverse('inventory:production_issue_queue'))
        blocks = _re.findall(r'(?s)<script(?![^>]*src)[^>]*>(.*?)</script>', html)
        out = pathlib.Path(r'C:\Users\HOSSEI~1\AppData\Local\Temp\kilo')
        out.mkdir(parents=True, exist_ok=True)
        path = out / 'rendered_queue.js'
        path.write_text('\n;\n'.join(blocks), encoding='utf-8')
        print('wrote', path, 'blocks:', len(blocks))

        # نمایش نواحی حساس
        joined = '\n'.join(blocks)
        m = _re.search(r'(?s)const paintWorkerIds.{0,120}', joined)
        print('--- paintWorkerIds region ---')
        print(m.group(0) if m else 'NOT FOUND')
        m = _re.search(r'(?s)const csrfToken.{0,160}', joined)
        print('--- csrfToken region ---')
        print(m.group(0) if m else 'NOT FOUND')

    def test_modal_dom_structure(self):
        """The preview modal must NOT be nested inside the issues card/table."""
        from html.parser import HTMLParser

        html = self.get(reverse('inventory:production_issue_queue'))

        class Structure(HTMLParser):
            VOID = {'meta', 'link', 'input', 'br', 'hr', 'img', 'source', 'path'}

            def __init__(self):
                super().__init__()
                self.stack = []
                self.paths = {}

            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if 'id' in d:
                    self.paths[d['id']] = list(self.stack)
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in self.VOID:
                    return
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass

        p = Structure()
        p.feed(html)
        print('\n===== MODAL ANCESTRY =====')
        print('handoverPreviewModal path:', p.paths.get('handoverPreviewModal'))
        print('issuesCard            path:', p.paths.get('issuesCard'))
        print('issuesTable           path:', p.paths.get('issuesTable'))
        print('handoverBar           path:', p.paths.get('handoverBar'))
        print('confirmHandoverBtn    path:', p.paths.get('confirmHandoverBtn'))

        modal_path = p.paths.get('handoverPreviewModal') or []
        print('modal inside issuesCard:', 'issuesCard' in modal_path or 'table-responsive' in modal_path)
        print('leftover open tags at end:', p.stack)

    def test_return_button_and_modal_contract(self):
        """
        Exact user sequence on the custody board.
        Verifies every contract the JS depends on.
        """
        import re

        issue = self.make_issue('3')
        self.post_json('inventory:handover_create', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'received_by': self.worker.pk,
            'held_by': self.worker.pk,
        })
        html = self.get(reverse('inventory:custody_board'))

        print('\n===== RETURN FLOW CONTRACT =====')

        # 1) دکمهٔ ثبت بازگشت باید با data-attributeهای لازم وجود داشته باشد
        btns = re.findall(r'(?s)<button[^>]*btn-custody-return[^>]*>', html)
        print('  return buttons found:', len(btns))
        self.assertEqual(len(btns), 1)
        b = btns[0]
        for attr in ('data-bs-toggle="modal"', 'data-bs-target="#custodyReturnModal"',
                     'data-raw=', 'data-holder=', 'data-quantity='):
            print(f'    has {attr:34}', attr in b)
            self.assertIn(attr, b, f'button missing {attr}')

        # 2) المان‌هایی که JS با getElementById صدا می‌زند باید موجود باشند
        for el in ('custodyReturnModal', 'custodyHolderName', 'custodyMaterialName',
                   'custodyCurrentQty', 'custodyUnit', 'custodyMeasuredInput',
                   'custodyNoteInput', 'custodyAlert', 'custodySaveBtn',
                   'custodyZeroBtn', 'custodyTable'):
            present = f'id="{el}"' in html
            print(f'    element {el:22}', present)
            self.assertTrue(present, f'missing element #{el}')

        # 3) اسکریپت باید به همین idها گوش بدهد
        print('\n  --- JS references ---')
        for el in ('custodyTable', 'custodySaveBtn', 'custodyZeroBtn',
                   'custodyReturnModal', 'custodyMeasuredInput'):
            ref = f"getElementById('{el}')" in html
            print(f'    JS binds {el:22}', ref)
            self.assertTrue(ref)

        # 4) target مودال باید واقعاً در صفحه باشد
        print('\n    data-bs-target matches real modal id:',
              'id="custodyReturnModal"' in html)
        self.assertIn('id="custodyReturnModal"', html)

    def test_custody_toggle_is_outside_rebuilt_region(self):
        """
        Regression: the custody checkbox MUST live outside #handoverPreviewContent.
        If renderPreview() rebuilds it, the user's tick is destroyed instantly.
        """
        import re

        html = self.get(reverse('inventory:production_issue_queue'))

        # متن داخل #handoverPreviewContent را استخراج کن و ببین عناصر فرم
        # داخلش هستند یا نه.
        start = html.index('id="handoverPreviewContent"')
        depth = 0
        i = start
        while i < len(html):
            nxt_open = html.find('<div', i + 1)
            nxt_close = html.find('</div>', i + 1)
            if nxt_close == -1:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                if depth == 0:
                    break
                depth -= 1
                i = nxt_close + 6
        rebuild_region = html[start:i]

        print('\n===== CHECKBOX PLACEMENT =====')
        print('  rebuilt region length:', len(rebuild_region))
        for el in ('handoverCustodyToggle', 'handoverReceiverSelect',
                   'handoverNoteInput', 'previewAlert'):
            inside = f'id="{el}"' in rebuild_region
            print(f'  {el:24} inside rebuilt region: {inside}')
            self.assertFalse(inside, f'{el} must NOT be inside the rebuilt preview region')

        # و در عوض باید داخل مودال و بیرون از ناحیهٔ بازسازی باشند
        self.assertIn('id="handoverCustodyToggle"', html)
        self.assertIn('id="handoverFormArea"', html)

    def test_custody_board_dom_structure(self):
        """The return modal must not be nested inside the custody table."""
        from html.parser import HTMLParser

        html = self.get(reverse('inventory:custody_board'))

        class Tree(HTMLParser):
            VOID = {'meta', 'link', 'input', 'br', 'hr', 'img', 'source', 'path'}

            def __init__(self):
                super().__init__()
                self.stack = []
                self.paths = {}

            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if 'id' in d:
                    self.paths[d['id']] = list(self.stack)
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in self.VOID:
                    return
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass

        t = Tree()
        t.feed(html)
        modal_path = t.paths.get('custodyReturnModal') or []
        print('\n===== BOARD MODAL ANCESTRY =====')
        print('  modal path:', modal_path)
        print('  nested in table:', 'table' in modal_path)
        self.assertNotIn('custodyTable', modal_path)
        self.assertEqual(t.stack, [], 'unclosed tags present')

    def test_issue_material_form_supports_custody(self):
        """The per-row quick-delivery form must also be able to create custody."""
        issue = self.make_issue('3')
        req = Request(
            self.live_server_url + reverse('inventory:issue_material', args=[issue.pk]),
            data=urlencode({
                'quantity': '3',
                'held_by': self.worker.pk,
                'csrfmiddlewaretoken': self.csrf(),
            }).encode(),
            headers={'Referer': self.live_server_url + reverse('inventory:production_issue_queue')},
            method='POST',
        )
        resp = self.opener.open(req)
        print('[8] per-row form status:', resp.status, resp.geturl())
        print('    custody:', list(MaterialCustody.objects.values()))
        print('    leftover:', list(MaterialLeftover.objects.values()))

        html = self.get(reverse('inventory:custody_board'))
        print('    board rows:', re.findall(r'data-custody="(\d+)"', html))
        self.assertTrue(MaterialCustody.objects.filter(held_by=self.worker).exists())

    def test_issue_material_form_without_custody(self):
        issue = self.make_issue('3')
        req = Request(
            self.live_server_url + reverse('inventory:issue_material', args=[issue.pk]),
            data=urlencode({
                'quantity': '3',
                'held_by': '',
                'csrfmiddlewaretoken': self.csrf(),
            }).encode(),
            headers={'Referer': self.live_server_url + reverse('inventory:production_issue_queue')},
            method='POST',
        )
        self.opener.open(req).read()
        print('[9] per-row (no custody) -> legacy')
        print('    custody:', list(MaterialCustody.objects.values()))
        print('    leftover:', list(MaterialLeftover.objects.values()))
        self.assertFalse(MaterialCustody.objects.exists())
        self.assertEqual(MaterialLeftover.objects.get().quantity, Decimal('1.50'))

    def test_issue_material_form_invalid_holder(self):
        issue = self.make_issue('3')
        req = Request(
            self.live_server_url + reverse('inventory:issue_material', args=[issue.pk]),
            data=urlencode({
                'quantity': '3',
                'held_by': 'not-a-number',
                'csrfmiddlewaretoken': self.csrf(),
            }).encode(),
            headers={'Referer': self.live_server_url + reverse('inventory:production_issue_queue')},
            method='POST',
        )
        self.opener.open(req).read()
        print('[10] invalid holder -> falls back to legacy, no crash')
        self.assertFalse(MaterialCustody.objects.exists())

    def test_return_with_form_encoded(self):
        issue = self.make_issue('3')
        self.post_json('inventory:handover_create', {
            'items': [{'issue_id': issue.pk, 'quantity': '3'}],
            'received_by': self.worker.pk,
            'held_by': self.worker.pk,
        })
        req = Request(
            self.live_server_url + reverse('inventory:custody_return'),
            data=urlencode({
                'raw_material_id': self.raw.pk,
                'held_by': self.worker.pk,
                'measured_quantity': '2.5',
            }).encode(),
            headers={
                'X-CSRFToken': self.csrf(),
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': self.live_server_url + reverse('inventory:custody_return'),
            },
            method='POST',
        )
        resp = self.opener.open(req)
        print('[7] form return status:', resp.status, resp.read().decode()[:160])
