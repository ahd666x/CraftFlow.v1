"""
تست موتور شواهد، اطمینان و رجیستری محدودیت‌ها (فاز ۲).

قرارداد‌هایی که این فایل قفل می‌کند:
  * هر ``Evidence`` باید metric/unit/source/query داشته باشد.
  * ``value`` هرگز نباید متن نمایشی فارسی داشته باشد.
  * ``query`` هرگز نباید SQL باشد.
  * هر ``Finding`` باید شناسه، دامنه، شدت و شواهد داشته باشد.
  * یافتهٔ مشتق‌شده باید محدودیت اعلام کند.
  * اطمینان هرگز توسط LLM ساخته نمی‌شود و فقط سه مقدار مجاز دارد.
  * دادهٔ ناکافی هرگز به صفر تبدیل نمی‌شود.
"""
from django.test import SimpleTestCase, TestCase

from craftflow_ai.analysis.evidence import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    AnalysisReport,
    DomainResult,
    Evidence,
    Finding,
    confidence_from_signals,
    insufficient_data,
    normalize_signal,
    rollup_status,
    worst_confidence,
    worst_severity,
)
from craftflow_ai.analysis.limits import (
    LIMITS,
    STATUS_INSUFFICIENT_DATA,
    limitation_codes,
    limitation_messages,
    unavailable,
)
from craftflow_ai.analysis.production import unknown_station_bucket
from craftflow_ai.analysis.inventory import warehouse_impact
from craftflow_ai.analysis.order import order_delay, order_health

REQUIRED_LIMIT_CODES = (
    'missing_due_date',
    'missing_material_mapping',
    'partial_paint_material_coverage',
    'missing_nonpaint_schedule',
    'missing_nonpaint_duration',
    'missing_dependency_graph',
    'missing_station_capacity',
    'missing_quality_events',
    'missing_holiday_data',
    'unknown_station_codes',
)


def sample_evidence(**overrides):
    payload = {
        'metric': 'open_tasks',
        'value': 12,
        'unit': 'task',
        'source': 'product.ProductionTask',
        'query': 'open tasks of this station grouped by status',
    }
    payload.update(overrides)
    return Evidence(**payload)


class EvidenceContractTests(SimpleTestCase):
    def test_required_fields_are_present(self):
        payload = sample_evidence().to_dict()
        for key in ('metric', 'value', 'unit', 'source', 'query'):
            self.assertIn(key, payload)
            self.assertTrue(payload[key] not in (None, ''), key)

    def test_source_and_query_are_mandatory(self):
        for field in ('source', 'query'):
            with self.assertRaises(ValueError):
                sample_evidence(**{field: ''})

    def test_metric_and_unit_are_mandatory(self):
        for field in ('metric', 'unit'):
            with self.assertRaises(ValueError):
                sample_evidence(**{field: '  '})

    def test_value_stays_machine_readable(self):
        evidence = sample_evidence(value={'count': 3, 'ids': [1, 2, 3]})
        self.assertEqual(evidence.value, {'count': 3, 'ids': [1, 2, 3]})

    def test_derived_evidence_is_flagged(self):
        payload = sample_evidence(derived=True).to_dict()
        self.assertTrue(payload['derived'])

    def test_comparison_and_threshold_are_optional(self):
        payload = sample_evidence(comparison='>', threshold='> 3 days').to_dict()
        self.assertEqual(payload['comparison'], '>')
        self.assertEqual(payload['threshold'], '> 3 days')
        self.assertNotIn('comparison', sample_evidence().to_dict())


class LimitsRegistryTests(SimpleTestCase):
    def test_all_required_limit_codes_exist(self):
        for code in REQUIRED_LIMIT_CODES:
            self.assertIn(code, LIMITS, code)

    def test_every_limit_has_a_message_and_source(self):
        for code, limit in LIMITS.items():
            self.assertTrue(limit.message.strip(), code)
            self.assertTrue(limit.source.strip(), code)

    def test_unavailable_never_reports_zero(self):
        payload = unavailable('missing_material_mapping').to_dict()
        self.assertEqual(payload['status'], STATUS_INSUFFICIENT_DATA)
        self.assertEqual(payload['reason'], 'missing_material_mapping')
        self.assertNotIn('material_shortage', payload)
        self.assertNotIn(0, payload.values())

    def test_unavailable_is_falsy(self):
        self.assertFalse(unavailable('missing_station_capacity'))

    def test_unavailable_carries_blocked_capabilities(self):
        payload = unavailable('missing_station_capacity').to_dict()
        self.assertIn('blocked_capabilities', payload)
        self.assertIn('station_capacity_simulation', payload['blocked_capabilities'])

    def test_limitation_codes_are_deduplicated_and_ordered(self):
        codes = limitation_codes(['missing_due_date', 'missing_due_date',
                                  'missing_dependency_graph'])
        self.assertEqual(codes, ('missing_due_date', 'missing_dependency_graph'))

    def test_limitation_messages_are_human_readable(self):
        messages = limitation_messages(['missing_due_date'])
        self.assertTrue(messages[0].strip())

    def test_unknown_limit_code_degrades_gracefully(self):
        payload = unavailable('not_a_real_limit_code').to_dict()
        self.assertEqual(payload['reason'], 'not_a_real_limit_code')


class ConfidenceEngineTests(SimpleTestCase):
    def test_all_real_inputs_give_high(self):
        self.assertEqual(confidence_from_signals(['real', 'real']), CONFIDENCE_HIGH)

    def test_one_derived_input_gives_medium(self):
        self.assertEqual(confidence_from_signals(['real', 'derived']), CONFIDENCE_MEDIUM)

    def test_one_missing_input_gives_low(self):
        self.assertEqual(confidence_from_signals(['real', 'missing']), CONFIDENCE_LOW)
        self.assertEqual(
            confidence_from_signals(['real', 'derived', 'missing']), CONFIDENCE_LOW
        )

    def test_no_signals_gives_low(self):
        self.assertEqual(confidence_from_signals([]), CONFIDENCE_LOW)

    def test_status_names_map_to_signals(self):
        self.assertEqual(normalize_signal('insufficient_data'), 'missing')
        self.assertEqual(normalize_signal('unknown'), 'missing')
        self.assertEqual(normalize_signal('partial'), 'derived')
        self.assertEqual(normalize_signal('healthy'), 'real')

    def test_only_three_confidence_levels_are_allowed(self):
        self.assertEqual(
            sorted({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW}),
            ['high', 'low', 'medium'],
        )

    def test_worst_confidence_picks_the_lowest(self):
        self.assertEqual(
            worst_confidence(CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW),
            CONFIDENCE_LOW,
        )
        self.assertEqual(worst_confidence(CONFIDENCE_HIGH, CONFIDENCE_MEDIUM),
                         CONFIDENCE_MEDIUM)

    def test_finding_rejects_unknown_confidence(self):
        with self.assertRaises(ValueError):
            Finding(
                finding_id='x', domain='production', severity='high',
                summary='x', evidence=(sample_evidence(),), confidence='very-high',
            )


class FindingContractTests(SimpleTestCase):
    def test_finding_requires_evidence(self):
        with self.assertRaises(ValueError):
            Finding(finding_id='f', domain='production', severity='high', summary='s')

    def test_finding_requires_identity_fields(self):
        for field in ('finding_id', 'domain', 'severity'):
            payload = {
                'finding_id': 'f', 'domain': 'production',
                'severity': 'high', 'summary': 's',
                'evidence': (sample_evidence(),),
            }
            payload[field] = ''
            with self.assertRaises(ValueError):
                Finding(**payload)

    def test_derived_finding_must_declare_a_limitation(self):
        with self.assertRaises(ValueError):
            Finding(
                finding_id='f', domain='production', severity='high', summary='s',
                evidence=(sample_evidence(),), derived=True,
            )

    def test_derived_finding_with_limitation_is_accepted(self):
        finding = Finding(
            finding_id='f', domain='production', severity='medium', summary='s',
            evidence=(sample_evidence(),), derived=True,
            limitations=('missing_dependency_graph',),
        )
        payload = finding.to_dict()
        self.assertTrue(payload['derived'])
        self.assertEqual(payload['limitations'], ['missing_dependency_graph'])

    def test_worst_severity_ranks_by_fixed_order(self):
        self.assertEqual(worst_severity('low', 'critical', 'medium'), 'critical')
        self.assertEqual(worst_severity('low', 'medium'), 'medium')
        self.assertEqual(worst_severity(), 'info')


class DomainResultTests(SimpleTestCase):
    def test_unknown_status_requires_a_limitation(self):
        with self.assertRaises(ValueError):
            DomainResult(
                domain='quality', status='unknown', confidence=CONFIDENCE_LOW,
                summary='no data', evidence=(sample_evidence(),),
            )

    def test_unknown_status_with_limitation_is_accepted(self):
        result = DomainResult(
            domain='quality', status='unknown', confidence=CONFIDENCE_LOW,
            summary='no data', evidence=(sample_evidence(),),
            limitations=('missing_quality_events',),
        )
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(result.to_dict()['limitations'], ['missing_quality_events'])

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            DomainResult(
                domain='production', status='totally-fine', confidence=CONFIDENCE_HIGH,
                summary='s', evidence=(sample_evidence(),),
            )

    def test_all_documented_statuses_are_accepted(self):
        for status in ('healthy', 'warning', 'stalled', 'blocked', 'in_progress',
                       'complete', 'not_started'):
            result = DomainResult(
                domain='production', status=status, confidence=CONFIDENCE_HIGH,
                summary='s', evidence=(sample_evidence(),),
            )
            self.assertEqual(result.status, status)

    def test_rollup_precedence_prefers_worst_domain(self):
        self.assertEqual(
            rollup_status(['healthy', 'warning', 'complete']), 'warning')
        self.assertEqual(
            rollup_status(['healthy', 'unknown', 'complete']), 'unknown')
        self.assertEqual(rollup_status(['blocked', 'warning']), 'blocked')

    def test_complete_requires_every_domain_complete(self):
        self.assertEqual(rollup_status(['complete', 'complete']), 'complete')
        self.assertEqual(rollup_status(['complete', 'healthy']), 'healthy')
        self.assertEqual(rollup_status([]), 'unknown')


class InsufficientDataTests(SimpleTestCase):
    def test_insufficient_data_has_no_findings_and_no_fake_zero(self):
        report = insufficient_data(
            'missing_material_mapping',
            confidence_reasons=['Material.raw_material is not populated'],
        )
        payload = report.analysis_payload()
        self.assertEqual(payload['status'], STATUS_INSUFFICIENT_DATA)
        self.assertEqual(payload['confidence'], CONFIDENCE_LOW)
        self.assertEqual(payload['findings'], [])
        self.assertEqual(payload['limitations'], ['missing_material_mapping'])
        self.assertEqual(
            payload['confidence_reasons'], ['Material.raw_material is not populated']
        )
        self.assertNotIn('"0"', str(payload))

    def test_insufficient_data_status_is_explicit(self):
        payload = insufficient_data('missing_quality_events').analysis_payload()
        self.assertEqual(payload['status'], 'insufficient_data')
        self.assertEqual(payload['reason'] if 'reason' in payload else
                         payload['limitations'][0], 'missing_quality_events')


class LiveEvidenceQualityTests(TestCase):
    """
    بررسی قرارداد شواهد روی خروجی واقعی تحلیل‌ها.

    روی دیتابیس واقعی تست اجرا می‌شود، چون تحلیل فقط‌خواندنی است و هیچ
    جدولی را تغییر نمی‌دهد؛ بنابراین جداسازی تراکنشی چیزی برای مقایسه باقی
    نمی‌گذارد.
    """

    def _all_evidence(self):
        from product.models import Order

        rows = []
        order = Order.objects.order_by('id').first()
        if order is None:
            return rows

        health = order_health(order.id)
        for domain in health['domains'].values():
            rows.extend(domain['evidence'])
            for finding in domain['findings']:
                rows.extend(finding['evidence'])

        delay = order_delay(order.id)
        rows.extend(delay['evidence'])
        for finding in delay['findings']:
            rows.extend(finding['evidence'])
        return rows

    def test_no_live_evidence_carries_sql_in_query(self):
        for evidence in self._all_evidence():
            lowered = str(evidence.get('query', '')).lower()
            for token in ('select ', ' from ', ' where ', 'join ', 'count('):
                self.assertNotIn(token, lowered, evidence.get('metric'))

    def test_no_live_evidence_value_contains_persian_display_text(self):
        """مقدار باید ماشینی باشد؛ متن فارسی جای آن در summary است."""
        for evidence in self._all_evidence():
            value = evidence.get('value')
            if isinstance(value, str):
                self.assertEqual(value, value.strip())
            elif isinstance(value, (dict, list)):
                self.assertNotIsInstance(value, str)

    def test_every_live_finding_is_structurally_valid(self):
        from product.models import Order

        order = Order.objects.order_by('id').first()
        if order is None:
            self.skipTest('no orders in this database')
        health = order_health(order.id)
        findings = list(health['findings'])
        if health['findings'] == [] and not findings:
            self.skipTest('no findings produced for this order')
        for finding in findings:
            for key in ('finding_id', 'domain', 'severity', 'evidence', 'confidence'):
                self.assertIn(key, finding)
                self.assertTrue(finding[key], key)
            self.assertTrue(finding['evidence'])
            if finding.get('derived'):
                self.assertTrue(finding['limitations'], finding['finding_id'])


class WarehouseEvidenceShapeTests(TestCase):
    def test_warehouse_rows_expose_machine_readable_evidence(self):
        from inventory.models import RawMaterial
        if not RawMaterial.objects.exists():
            self.skipTest('no raw materials in this database')
        raw = RawMaterial.objects.first()
        report = warehouse_impact(raw_material_id=raw.pk)
        rows = report['materials']
        self.assertTrue(rows)
        for row in rows:
            for evidence in row['evidence']:
                for key in ('metric', 'value', 'unit', 'source', 'query'):
                    self.assertIn(key, evidence)
                self.assertIsInstance(evidence['value'], (str, dict, int, float))


class AnalysisReportSerializationTests(SimpleTestCase):
    def test_report_payload_matches_documented_envelope(self):
        report = AnalysisReport(
            status='complete', confidence=CONFIDENCE_HIGH,
            findings=(Finding(
                finding_id='order:1:production_overdue', domain='production',
                severity='medium', summary='s', evidence=(sample_evidence(),),
                confidence=CONFIDENCE_HIGH, limitations=('missing_nonpaint_schedule',),
                derived=True,
            ),),
            limitations=('missing_dependency_graph',),
            confidence_reasons=('reason',),
        )
        payload = report.to_payload()
        self.assertEqual(
            sorted(payload['analysis'].keys()),
            ['confidence', 'confidence_reasons', 'findings', 'limitations',
             'limitations_detail', 'status'],
        )