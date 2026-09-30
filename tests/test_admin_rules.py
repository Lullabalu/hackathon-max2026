"""Strict reviewed data, isolated calculators, and transactional admin revisions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import os
from threading import Barrier
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from ip_bot.activities import default_catalog, suggest_activity, validate_catalog
from ip_bot.admin import create_admin_app
from ip_bot.services import Services, content_template, preview_content, validate_content
from ip_bot.settings import Settings
from ip_bot.storage import Store
from ip_bot.tax import compare_taxes, default_rules, validate_rules


ANSWERS = {'activity': 'Разработка сайтов', 'region': 'Самарская область', 'revenue': '100000',
           'expenses': '0', 'employees': 0, 'clients': 'Физлица', 'npd_deduction_remaining': 0, 'year': 2026}


def npd(result):
    return next(row for row in result['rows'] if row['id'] == 'npd')


def changed_rules():
    rules = default_rules()
    rules['version'] = '2026-test-2'
    rules['rules']['npd']['person_rate'] = '0.05'
    return rules


class RuleValidationTests(unittest.TestCase):
    def test_defaults_validate_and_are_detached(self):
        for kind, factory, validator in (('tax_rules', default_rules, validate_rules), ('activities', default_catalog, validate_catalog)):
            source = factory()
            result = validator(source)
            self.assertEqual(result, source)
            self.assertIsNot(result, source)
            result.clear()
            self.assertTrue(source)
            self.assertTrue(factory())
            self.assertEqual(validate_content(kind, source), source)

    def test_tax_schema_rejects_extra_missing_and_wrong_types(self):
        mutations = [
            lambda x: x.update(unreviewed_code='anything'),
            lambda x: x.pop('version'),
            lambda x: x['rules']['usn'].update(income_rate=0.06),
            lambda x: x['rules']['npd'].update(employees_limit=True),
            lambda x: x['rules']['npd'].update(person_rate='NaN'),
            lambda x: x['rules']['npd'].update(person_rate='-0.04'),
            lambda x: x['rules']['npd'].update(person_rate='1e-2'),
            lambda x: x['rules']['npd'].update(person_rate='1.01'),
            lambda x: x['rules']['npd'].update(revenue_limit='0'),
            lambda x: x['rules']['npd'].update(person_deduction_rate='0.50'),
            lambda x: x.update(verified_on='2999-01-01'),
            lambda x: x.update(verified_on='20260930'),
        ]
        for mutate in mutations:
            payload = default_rules()
            mutate(payload)
            with self.subTest(payload=payload.get('version')), self.assertRaises(ValueError):
                validate_rules(payload)

    def test_data_cannot_enable_unsupported_year_or_formula(self):
        mutations = [lambda x: x.update(supported_years=[2027]),
                     lambda x: x['rules']['ausn'].update(tax_period='year'),
                     lambda x: x['rules']['npd'].update(own_contributions='57390'),
                     lambda x: x['rules']['patent'].update(check_previous_year=False)]
        for mutate in mutations:
            payload = default_rules()
            mutate(payload)
            with self.assertRaises(ValueError):
                validate_rules(payload)

    def test_thresholds_and_progressive_brackets_are_validated(self):
        payload = default_rules()
        payload['rules']['vat']['usn_exemption_limit'] = '99999999999'
        with self.assertRaises(ValueError):
            validate_rules(payload)
        for brackets in ([{'up_to': '1000', 'rate': '0.13'}],
                         [{'up_to': '2000', 'rate': '0.15'}, {'up_to': '1000', 'rate': '0.20'}, {'up_to': None, 'rate': '0.25'}],
                         [{'up_to': '1000', 'rate': '0.15'}, {'up_to': None, 'rate': '0.10'}]):
            payload = default_rules()
            payload['rules']['osno']['resident_ndfl_brackets'] = brackets
            with self.assertRaises(ValueError):
                validate_rules(payload)

    def test_source_urls_reject_external_hosts_userinfo_and_unsafe_schemes(self):
        bad = ['http://www.nalog.gov.ru/', 'https://nalog.gov.ru.evil.test/', 'https://@nalog.gov.ru/',
               'https://user@nalog.gov.ru/', 'https://nalog.gov.ru:8443/', 'file:///tmp/rules',
               'https://127.0.0.1/', 'https://nalog.gov.ru\\@evil.test/', 'https://nalog.gov.ru/\n']
        for url in bad:
            with self.subTest(url=url):
                taxes = default_rules()
                taxes['rules']['npd']['sources'] = [url]
                with self.assertRaises(ValueError):
                    validate_rules(taxes)
                catalog = default_catalog()
                catalog['sources']['software']['url'] = url
                with self.assertRaises(ValueError):
                    validate_catalog(catalog)

    def test_catalog_rejects_unmatchable_ids_duplicates_and_dangling_sources(self):
        mutations = [
            lambda x: x['activities'].update(extra=deepcopy(x['activities']['software'])),
            lambda x: x['activities'].pop('software'),
            lambda x: x['activities']['software'].update(regex='.*'),
            lambda x: x['activities']['software'].update(code=x['activities']['it_consulting']['code']),
            lambda x: x['activities']['software'].update(code='62'),
            lambda x: x['activities']['software'].update(sources=['missing']),
            lambda x: x['restrictions']['spirits_retail'].update(warnings=[]),
            lambda x: x['sources'].pop('alcohol'),
            lambda x: x.update(schema_version=True),
            lambda x: x.update(official_search='https://example.org/'),
        ]
        for mutate in mutations:
            payload = default_catalog()
            mutate(payload)
            with self.assertRaises(ValueError):
                validate_catalog(payload)

    def test_custom_rules_change_calculation_and_details_without_changing_defaults(self):
        payload = changed_rules()
        original = deepcopy(payload)
        revised = compare_taxes(ANSWERS, rules=payload)
        self.assertEqual(npd(revised)['total'], '5000.00')
        self.assertIn('5% с физлиц', ' '.join(npd(revised)['details']))
        self.assertEqual(revised['rules_version'], '2026-test-2')
        self.assertEqual(npd(compare_taxes(ANSWERS))['total'], '4000.00')
        self.assertEqual(payload, original)

    def test_parallel_rule_snapshots_cannot_leak_between_requests(self):
        import ip_bot.tax as tax
        barrier, real = Barrier(2), tax._npd

        def synchronized(*args):
            barrier.wait(timeout=5)
            return real(*args)

        with patch('ip_bot.tax._npd', side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(compare_taxes, ANSWERS, rules=rules) for rules in (default_rules(), changed_rules())]
            self.assertEqual([npd(future.result())['total'] for future in futures], ['4000.00', '5000.00'])
        self.assertEqual(npd(compare_taxes(ANSWERS))['total'], '4000.00')

    def test_custom_catalog_changes_selected_card_only_for_that_call(self):
        payload = default_catalog()
        title = payload['activities']['software']['title'] + ' — проверенная редакция'
        payload['activities']['software']['title'] = title
        self.assertEqual(suggest_activity('Разрабатываю сайты', catalog=payload)['primary']['title'], title)
        self.assertNotEqual(suggest_activity('Разрабатываю сайты')['primary']['title'], title)
        self.assertEqual(suggest_activity('Продаю водку', catalog=payload)['status'], 'restricted')

    def test_preview_uses_synthetic_examples_and_reports_changed_paths(self):
        result = preview_content('tax_rules', changed_rules())
        self.assertIn('rules.npd.person_rate', result['changed_fields'])
        self.assertEqual(len(result['examples']), 2)
        self.assertNotEqual(npd(result['examples'][0]['before'])['total'], npd(result['examples'][0]['after'])['total'])
        catalog = preview_content('activities', default_catalog())
        self.assertEqual(len(catalog['examples']), 3)
        self.assertEqual(catalog['changed_fields'], [])
        self.assertEqual(catalog['examples'][-1]['after']['status'], 'restricted')

    def test_templates_and_preview_routes_require_auth_before_storage(self):
        factory = Mock()
        token = 'test-admin-rules-' + 'x' * 32
        with patch.dict(os.environ, {'ADMIN_TOKEN': token}), TestClient(create_admin_app(Settings(), factory)) as client:
            factory.reset_mock()
            self.assertEqual(client.get('/api/templates/tax_rules').status_code, 401)
            self.assertEqual(client.post('/api/preview', json={'kind': 'tax_rules', 'payload': default_rules()}).status_code, 401)
            factory.assert_not_called()
            response = client.get('/api/templates/activities', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), default_catalog())
            factory.assert_not_called()


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужен TEST_DATABASE_URL с отдельной тестовой БД')
class AdminRuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'test_admin_rules_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.store = None
        self.addCleanup(self.cleanup)
        self.dsn = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')
        self.store = Store(self.dsn, worker=False)
        self.service = Services(self.store)

    def cleanup(self):
        if self.store is not None:
            self.store.close()
        try:
            self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def test_draft_preview_publish_and_rollback_for_both_kinds(self):
        for kind, payload in (('tax_rules', default_rules()), ('activities', default_catalog())):
            with self.subTest(kind=kind):
                one = self.service.create_draft(kind, payload)
                self.assertIsNone(self.service.published(kind))
                preview = self.service.preview_revision(one['id'])
                self.assertTrue(preview['preview']['valid'])
                self.assertIsNone(self.service.published(kind))
                self.service.publish(one['id'])
                changed = deepcopy(payload)
                if kind == 'tax_rules':
                    changed = changed_rules()
                else:
                    changed['activities']['software']['title'] += ' — редакция 2'
                two = self.service.create_draft(kind, changed)
                self.assertEqual(self.service.published(kind)['id'], one['id'])
                self.service.publish(two['id'])
                self.assertEqual(self.service.published(kind)['payload'], changed)
                self.assertEqual(self.service.revision(one['id'])['status'], 'superseded')
                self.service.rollback(kind, one['id'])
                self.assertEqual(self.service.published(kind)['payload'], payload)
                with self.assertRaises(ValueError):
                    self.service.enqueue_news(one['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) AS count FROM service_outbox').fetchone()['count'], 0)

    def test_publish_revalidates_and_failed_audit_does_not_change_publication(self):
        one = self.service.create_draft('tax_rules', default_rules())
        self.service.publish(one['id'])
        two = self.service.create_draft('tax_rules', changed_rules())
        invalid = changed_rules()
        invalid['supported_years'] = [2027]
        self.store.db.execute('UPDATE knowledge_revisions SET payload=%s WHERE id=%s', (Jsonb(invalid), two['id']))
        with self.assertRaises(ValueError):
            self.service.publish(two['id'])
        self.assertEqual(self.service.published('tax_rules')['id'], one['id'])
        self.store.db.execute('UPDATE knowledge_revisions SET payload=%s WHERE id=%s', (Jsonb(changed_rules()), two['id']))
        with patch.object(self.service, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.service.publish(two['id'])
        self.assertEqual(self.service.published('tax_rules')['id'], one['id'])
        self.assertEqual(self.service.revision(two['id'])['status'], 'draft')

    def test_admin_http_preview_draft_publish_and_rollback(self):
        token = 'test-admin-rules-' + 'x' * 32
        with patch.dict(os.environ, {'ADMIN_TOKEN': token}), TestClient(create_admin_app(Settings(database_url=self.dsn))) as client:
            headers = {'Authorization': 'Bearer ' + token}
            payload = {'kind': 'tax_rules', 'payload': changed_rules()}
            preview = client.post('/api/preview', headers=headers, json=payload)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertIn('rules.npd.person_rate', preview.json()['changed_fields'])
            self.assertEqual(client.get('/api/revisions', headers=headers).json(), [])
            response = client.post('/api/drafts', headers=headers, json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            ident = response.json()['id']
            self.assertEqual(client.get(f'/api/revisions/{ident}/preview', headers=headers).status_code, 200)
            self.assertEqual(client.post(f'/api/revisions/{ident}/publish', headers=headers).status_code, 200)
            self.assertEqual(client.post('/api/rollback', headers=headers, json={'kind': 'tax_rules', 'revision_id': ident}).status_code, 200)
            self.assertEqual(client.get('/api/published/tax_rules', headers=headers).json()['payload'], payload['payload'])
            bad = deepcopy(payload)
            bad['payload']['rules']['npd']['person_rate'] = 'NaN'
            self.assertEqual(client.post('/api/preview', headers=headers, json=bad).status_code, 400)


if __name__ == '__main__':
    unittest.main()
