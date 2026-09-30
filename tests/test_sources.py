from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from pypdf import PdfWriter

from ip_bot.budget import BudgetExpired, time_limit
from ip_bot.llm import Plan
from ip_bot.settings import Settings
from ip_bot.sources import EXTRACTOR_VERSION, Catalog, PinnedHTTPS, Researcher, SafeFetcher, SourceError, URLPolicy, chunks, extract_document


class SourcesTests(unittest.TestCase):
    def test_shared_deadline_interrupts_blocked_dns_without_network(self):
        policy = URLPolicy(['nalog.gov.ru'])
        with patch('ip_bot.sources.socket.getaddrinfo', side_effect=lambda *args, **kwargs: time.sleep(1)):
            started = time.monotonic()
            with self.assertRaises(BudgetExpired), time_limit(0.04):
                policy.addresses('nalog.gov.ru')
            self.assertLess(time.monotonic() - started, 0.5)

    def test_connection_retries_use_the_same_remaining_budget(self):
        raw, tls, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = tls
        timeouts = []

        def connect(address, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                time.sleep(0.02)
                raise OSError('first address unavailable')
            return raw

        with patch('ip_bot.sources.socket.create_connection', side_effect=connect), time_limit(0.5):
            connection = PinnedHTTPS('nalog.gov.ru', ['8.8.8.8', '8.8.4.4'], context)
            self.assertLessEqual(connection.timeout, 0.5)
            connection.connect()
        self.assertEqual(len(timeouts), 2)
        self.assertLess(timeouts[1], timeouts[0])
        self.assertLessEqual(raw.settimeout.call_args.args[0], timeouts[1])
        self.assertIs(connection.sock, tls)

    def test_tls_interruption_closes_raw_socket_and_propagates_budget(self):
        raw, context = Mock(), Mock()
        context.wrap_socket.side_effect = BudgetExpired()
        with patch('ip_bot.sources.socket.create_connection', return_value=raw):
            connection = PinnedHTTPS('nalog.gov.ru', ['8.8.8.8'], context)
            with self.assertRaises(BudgetExpired):
                connection.connect()
        raw.close.assert_called_once()

    def test_read_interruption_closes_http_resources_and_propagates_budget(self):
        fetcher = SafeFetcher(URLPolicy(['nalog.gov.ru']))
        response, connection = Mock(status=200), Mock()
        response.getheader.side_effect = lambda name, default=None: default
        response.read.side_effect = BudgetExpired()
        connection.getresponse.return_value = response
        with patch.object(fetcher.policy, 'addresses', return_value=['8.8.8.8']), \
             patch('ip_bot.sources.PinnedHTTPS', return_value=connection):
            with self.assertRaises(BudgetExpired):
                fetcher.raw('https://nalog.gov.ru/page')
        response.close.assert_called_once()
        connection.close.assert_called_once()

    def test_parser_does_not_hide_budget_expiration_as_bad_document(self):
        with patch('ip_bot.sources.BeautifulSoup', side_effect=BudgetExpired()):
            with self.assertRaises(BudgetExpired):
                extract_document('https://nalog.gov.ru/page', 'text/html', b'<html></html>')

    def test_url_policy_rejects_external_private_and_malformed_urls(self):
        policy = URLPolicy(['nalog.gov.ru'])
        self.assertEqual(policy.normalize('https://WWW.NALOG.GOV.RU/a#x'), 'https://www.nalog.gov.ru/a')
        for url in ['http://www.nalog.gov.ru/', 'https://nalog.gov.ru.evil.com/', 'https://evil.com/',
                    'https://user@nalog.gov.ru/', 'https://@nalog.gov.ru/', 'https://nalog.gov.ru:8443/',
                    'https://nalog.gov.ru:bad/', 'https://127.0.0.1/', 'file:///etc/passwd',
                    'https://[::1', 'https://nalog.gov.ru/a\nb']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                policy.normalize(url)
        for addresses in [['127.0.0.1'], ['169.254.169.254'], ['8.8.8.8', '10.0.0.1'], ['::1']]:
            with patch('ip_bot.sources.socket.getaddrinfo', return_value=[(0, 0, 0, '', (ip, 443)) for ip in addresses]):
                with self.assertRaises(SourceError):
                    policy.addresses('www.nalog.gov.ru')

    def test_redirect_destination_robots_checked_before_request(self):
        fetcher = SafeFetcher(URLPolicy(['nalog.gov.ru']))
        response = Mock(status=302)
        response.getheader.side_effect = lambda name, default=None: '/blocked' if name == 'Location' else default
        connection = Mock()
        connection.getresponse.return_value = response
        checked = []
        def allowed(url):
            checked.append(url)
            if url.endswith('/blocked'):
                raise SourceError('robots_disallowed')
        with patch.object(fetcher, 'ensure_allowed', side_effect=allowed), \
             patch.object(fetcher.policy, 'addresses', return_value=['8.8.8.8']), \
             patch('ip_bot.sources.PinnedHTTPS', return_value=connection):
            with self.assertRaisesRegex(SourceError, 'robots_disallowed'):
                fetcher.fetch('https://nalog.gov.ru/start')
        self.assertEqual(len(checked), 2)
        self.assertEqual(connection.request.call_count, 1)

    def test_robots_missing_is_allowed_but_outage_is_not(self):
        for error, expected in [('source_http_404', None), ('source_http_500', 'robots_unavailable')]:
            fetcher = SafeFetcher(URLPolicy(['nalog.gov.ru']))
            with patch.object(fetcher, 'raw', side_effect=SourceError(error)):
                if expected:
                    with self.assertRaisesRegex(SourceError, expected):
                        fetcher.ensure_allowed('https://nalog.gov.ru/doc')
                else:
                    fetcher.ensure_allowed('https://nalog.gov.ru/doc')

    def test_html_and_docx_extract_text_and_links(self):
        html = '<title>Регистрация</title><nav>Реклама</nav><main><script>bad()</script><p>' + 'Документы для регистрации ИП. ' * 5 + '</p><a href="/form.pdf">Форма заявления</a></main>'
        doc = extract_document('https://nalog.gov.ru/page', 'text/html', html.encode())
        self.assertNotIn('bad()', doc.text)
        self.assertNotIn('Реклама', doc.text)
        self.assertEqual(doc.links[0]['url'], 'https://nalog.gov.ru/form.pdf')
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="urn:test"><w:t>' + 'Заявление о регистрации ИП. ' * 5 + '</w:t></w:document>')
        doc = extract_document('https://nalog.gov.ru/form.docx', 'application/octet-stream', data.getvalue())
        self.assertIn('Заявление', doc.text)

    def test_pdf_without_text_is_not_evidence(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        data = io.BytesIO()
        writer.write(data)
        with self.assertRaisesRegex(SourceError, 'document_has_no_text'):
            extract_document('https://nalog.gov.ru/scan.pdf', 'application/pdf', data.getvalue())

    def test_aspnet_form_and_region_modal_do_not_hide_document(self):
        html = '<form id="MainForm"><div class="content"><select><option>Москва</option></select></div><div class="wrap-content"><h1>Регистрация ИП</h1><p>' + 'Подайте заявление Р21001 и паспорт. ' * 4 + '</p></div></form>'
        document = extract_document('https://nalog.gov.ru/page', 'text/html', html.encode())
        self.assertIn('Р21001', document.text)
        self.assertNotIn('Москва', document.text)

    def test_fns_table_layout_keeps_article_and_sidebar_document_links_separate(self):
        # Структура create_business/ip/creation/registration/step2/: две правые
        # колонки таблиц, статья в box-r, список скачиваемых форм в column-right.
        html = '''<html><head><meta charset="utf-8"><title>Регистрация</title></head><body>
            <table class="t_table"><tr><td class="left">
                <ul><li><a href="/other-topic/">Соседняя тема в левом меню</a></li></ul>
            </td><td class="right">
                <div class="pagination">Шаги 1 2 3 4</div>
                <div class="box"><div class="box-l"><div class="box-r">
                    <div class="step_title"><span class="step">Шаг 2</span> Формируем пакет документов</div>
                    <div class="t_section-text">
                        <p>Порядок подготовки документов зависит от выбранного способа подачи заявления.</p>
                        <ul class="disc"><li><a href="/main-form.pdf">Заявление Р21001</a></li></ul>
                        <div class="attension">Требования к подписи приведены в инструкции.</div>
                    </div>
                </div></div></div>
                <div class="bottom_nav"><a href="/step3/">Следующий шаг</a></div>
            </td></tr></table>
            <table class="t_table"><tr><td class="right"><div class="column-right">
                <div class="blank-layout"><div class="box-docs">
                    <div class="doc-services-icon">Формы и бланки</div>
                    <ul class="form-blank"><li><a href="/sidebar-form.pdf">Уведомление об отказе от применения УСН</a></li></ul>
                </div></div>
            </div></td></tr></table>
        </body></html>'''
        document = extract_document('https://nalog.gov.ru/create_business/ip/creation/registration/step2/', 'text/html', html.encode())
        self.assertIn('Формируем пакет документов', document.text)
        self.assertIn('Р21001', document.text)
        self.assertIn('Требования к подписи', document.text)
        for excluded in ('Соседняя тема', 'Шаги 1 2 3 4', 'Следующий шаг', 'Формы и бланки', 'отказе от применения УСН'):
            self.assertNotIn(excluded, document.text)
        links = {link['url']: link['title'] for link in document.links}
        self.assertIn('https://nalog.gov.ru/main-form.pdf', links)
        self.assertEqual(links['https://nalog.gov.ru/sidebar-form.pdf'], 'Уведомление об отказе от применения УСН')

    def test_semantic_sidebar_links_survive_but_sidebar_text_is_not_evidence(self):
        html = '''<main><p>Основной материал страницы содержит подробное описание порядка подготовки и передачи документов.</p>
            <aside><a href="/related.pdf">Другой документ из боковой панели</a></aside>
            <div role="complementary"><a href="/more.pdf">Дополнительная форма</a></div>
        </main>'''
        document = extract_document('https://nalog.gov.ru/page', 'text/html', html.encode())
        self.assertIn('Основной материал', document.text)
        self.assertNotIn('Другой документ', document.text)
        self.assertNotIn('Дополнительная форма', document.text)
        self.assertEqual({link['url'] for link in document.links}, {'https://nalog.gov.ru/related.pdf', 'https://nalog.gov.ru/more.pdf'})

    def test_modern_fns_article_keeps_mfc_note_and_links_without_navigation(self):
        # Минимальная структура реальной /rn77/ip/interest/reg_ip/petition/:
        # статья в page-content__center, оглавление внутри, примечание в ul.
        html = '''<html><head><meta charset="utf-8"><title>Регистрация ИП</title></head><body>
            <div class="wrap-content">
                <div class="phone">8-800-222-2222</div>
                <div class="breadcrumbs">Главная страница Индивидуальные предприниматели</div>
                <div id="divSecondPageColumns" class="page-content">
                    <div class="page-content__left"><a href="/other/">Соседняя тема</a></div>
                    <div class="page-content__center"><div class="text_block">
                        <div class="content_list">Содержание страницы <a href="#steps">Оглавление шагов</a></div>
                        <h2 id="steps">Порядок регистрации индивидуального предпринимателя</h2>
                        <div class="text_block"><div class="with_icon"><ul class="ul_class">
                            <li>Подготовьте <a href="/application.pdf">заявление Р21001</a>.</li>
                            <li>Квитанция об уплате госпошлины в размере 800 рублей.</li>
                            <div class="note">При направлении документов для государственной регистрации
                                в форме электронных документов, в том числе через МФЦ и нотариуса,
                                уплачивать государственную пошлину не требуется!</div>
                        </ul></div></div>
                    </div></div>
                    <div class="page-content__right"><a href="/sidebar-form.pdf">Дополнительная форма из меню</a></div>
                </div>
            </div></body></html>'''
        document = extract_document('https://nalog.gov.ru/rn77/ip/interest/reg_ip/petition/', 'text/html', html.encode())
        for expected in ('Порядок регистрации', 'Р21001', '800 рублей', 'МФЦ и нотариуса',
                         'уплачивать государственную пошлину не требуется!'):
            self.assertIn(expected, document.text)
        for excluded in ('8-800-222-2222', 'Главная страница', 'Соседняя тема', 'Содержание страницы',
                         'Оглавление шагов', 'Дополнительная форма из меню'):
            self.assertNotIn(excluded, document.text)
        links = {link['url'] for link in document.links}
        self.assertIn('https://nalog.gov.ru/application.pdf', links)
        self.assertIn('https://nalog.gov.ru/sidebar-form.pdf', links)

    def test_chunk_overlap_preserves_boundary_text(self):
        parts = list(chunks('a' * 900 + 'регистрация' + 'b' * 1100))
        self.assertTrue(all(len(part) <= 1000 for part in parts))
        self.assertIn('регистрация', parts[1])

    def test_bounded_search_uses_cache_and_never_stale_failed_page(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / 'sources.json'
            file.write_text(json.dumps({'allowed_domains': ['nalog.gov.ru'], 'sources': [
                {'id': 'registration', 'title': 'Регистрация ИП', 'urls': ['https://nalog.gov.ru/a']}]}))
            store, fetcher = Mock(), Mock()
            store.document.return_value = {'fetched_at': datetime.now(timezone.utc), 'extractor_version': EXTRACTOR_VERSION, 'links': [
                {'url': 'https://evil.com/form', 'title': 'Регистрация'},
                {'url': 'https://nalog.gov.ru/b', 'title': 'Регистрация'}]}
            store.search_documents.return_value = []
            plan = Plan(query='Регистрация ИП', topics=['registration'], keywords=[], clarification='', relevant=True)
            researcher = Researcher(store, Settings(sources_file=file, max_pages=1), fetcher=fetcher)
            researcher.search(plan)
            fetcher.fetch.assert_not_called()
            self.assertEqual(store.search_documents.call_args.args[1], ['https://nalog.gov.ru/a'])
            store.document.return_value['fetched_at'] -= timedelta(days=3)
            fetcher.fetch.side_effect = SourceError('source_network_failed')
            _, errors = researcher.search(plan)
            self.assertEqual(errors, ['source_network_failed'])
            self.assertEqual(store.search_documents.call_args.args[1], [])

    def test_old_extractor_cache_is_refreshed_before_search(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / 'sources.json'
            file.write_text(json.dumps({'allowed_domains': ['nalog.gov.ru'], 'sources': [
                {'id': 'registration', 'title': 'Регистрация ИП', 'urls': ['https://nalog.gov.ru/a']}]}))
            store, fetcher = Mock(), Mock()
            store.document.return_value = {'fetched_at': datetime.now(timezone.utc), 'extractor_version': EXTRACTOR_VERSION - 1, 'links': []}
            store.search_documents.return_value = []
            html = '<main><p>' + 'Обновлённый текст статьи без боковых блоков. ' * 3 + '</p></main>'
            fetcher.fetch.return_value = ('https://nalog.gov.ru/a', 'text/html', html.encode())
            plan = Plan(query='Регистрация ИП', topics=['registration'], keywords=[], clarification='', relevant=True)
            researcher = Researcher(store, Settings(sources_file=file, max_pages=1), fetcher=fetcher)
            researcher.search(plan)
            fetcher.fetch.assert_called_once_with('https://nalog.gov.ru/a')
            self.assertIn('Обновлённый текст', store.save_document.call_args.args[0].text)
            self.assertEqual(store.search_documents.call_args.args[1], ['https://nalog.gov.ru/a'])


class ResearcherCacheTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.seed = 'https://nalog.gov.ru/registration'
        self.linked = 'https://nalog.gov.ru/details'
        self.tax_seed = 'https://nalog.gov.ru/taxes'
        filename = Path(directory.name) / 'sources.json'
        filename.write_text(json.dumps({'allowed_domains': ['nalog.gov.ru'], 'sources': [
            {'id': 'registration', 'title': 'Регистрация', 'urls': [self.seed]},
            {'id': 'taxes', 'title': 'Налоги', 'urls': [self.tax_seed]},
        ]}))
        self.documents = {self.seed: self.record(links=[{'url': self.linked, 'title': 'Документы регистрации'}])}
        self.matches = [{'url': self.seed, 'text': 'Документы для регистрации перечислены в инструкции.'}]
        self.store, self.fetcher = Mock(), Mock()
        self.store.document.side_effect = self.documents.get
        self.store.search_documents.side_effect = lambda query, urls, limit: [item for item in self.matches if item['url'] in urls][:limit]
        self.store.save_document.side_effect = lambda document: self.documents.update({document.url: self.record(links=document.links)})
        html = '<main><p>' + 'Документы для регистрации перечислены в отдельной инструкции. ' * 3 + '</p></main>'
        self.fetcher.fetch.side_effect = lambda url: (url, 'text/html', html.encode())
        self.settings = Settings(sources_file=filename, max_pages=2)
        self.researcher = Researcher(self.store, self.settings, fetcher=self.fetcher)
        self.plan = Plan(query='Документы для регистрации ИП', topics=['registration'], keywords=[], clarification='', relevant=True)

    @staticmethod
    def record(*, links=()):
        return {'fetched_at': datetime.now(timezone.utc), 'extractor_version': EXTRACTOR_VERSION, 'links': list(links)}

    def test_complete_fresh_seed_answer_avoids_following_uncached_links(self):
        matches, errors = self.researcher.search(self.plan)
        self.assertEqual(matches, self.matches)
        self.assertEqual(errors, [])
        self.fetcher.fetch.assert_not_called()
        self.store.search_documents.assert_called_once_with('Документы для регистрации ИП', [self.seed], limit=4)

    def test_low_budget_returns_completed_article_without_starting_next_page(self):
        self.documents.clear()
        remaining = [2.0]
        html = '<main><p>' + 'Документы регистрации перечислены в инструкции. ' * 3 + '</p><a href="/details">Документы регистрации</a></main>'

        def fetch(url):
            remaining[0] = 0.5
            return url, 'text/html', html.encode()

        self.fetcher.fetch.side_effect = fetch
        with patch('ip_bot.sources.remaining_seconds', side_effect=lambda: remaining[0]):
            matches, errors = self.researcher.search(self.plan)
        self.assertEqual(matches, self.matches)
        self.assertEqual(errors, ['search_budget_exhausted'])
        self.fetcher.fetch.assert_called_once_with(self.seed)
        self.assertEqual(self.store.search_documents.call_args.args[1], [self.seed])
        self.assertEqual(self.researcher.completed_searches, {})

    def test_low_budget_before_first_page_never_starts_http(self):
        self.documents.clear()
        with patch('ip_bot.sources.remaining_seconds', return_value=0.5):
            matches, errors = self.researcher.search(self.plan)
        self.assertEqual(matches, [])
        self.assertEqual(errors, ['search_budget_exhausted'])
        self.fetcher.fetch.assert_not_called()

    def test_seed_shortcut_preserves_exception_in_continuation_of_same_article(self):
        exception = {'url': self.seed, 'text': 'При подаче в электронной форме уплачивать пошлину не требуется.'}
        self.matches.append(exception)
        matches, errors = self.researcher.search(self.plan)
        self.assertEqual(matches, self.matches)
        self.assertIn(exception, matches)
        self.assertEqual(errors, [])
        self.fetcher.fetch.assert_not_called()

    def test_common_word_or_missing_query_condition_does_not_stop_crawl(self):
        for text in ('Документы о другом вопросе.', 'Документы для регистрации перечислены в инструкции.'):
            with self.subTest(text=text):
                self.matches[0]['text'] = text
                self.documents.pop(self.linked, None)
                self.fetcher.fetch.reset_mock()
                plan = self.plan.model_copy(update={'query': 'Документы для регистрации иностранца'})
                self.researcher.search(plan)
                self.fetcher.fetch.assert_called_once_with(self.linked)

    def test_form_numbers_require_exact_match_for_seed_shortcut(self):
        self.plan = self.plan.model_copy(update={'query': 'Заявление Р21001'})
        self.matches[0]['text'] = 'Заявление Р21002 приведено в приложении.'
        self.documents[self.seed]['links'][0]['title'] = 'Заявление Р21001'
        self.researcher.search(self.plan)
        self.fetcher.fetch.assert_called_once_with(self.linked)

    def test_numeric_condition_is_required_for_memo_even_when_two_other_words_match(self):
        plan = self.plan.model_copy(update={'query': 'УСН 6 процентов'})
        self.matches[0]['text'] = 'УСН 15 процентов описана на странице.'
        self.documents[self.seed]['links'][0]['title'] = 'УСН'
        self.researcher.search(plan)
        new_url = 'https://nalog.gov.ru/usn-six'
        self.documents[self.seed]['links'].insert(0, {'url': new_url, 'title': 'УСН 6 процентов'})
        self.fetcher.fetch.reset_mock()
        self.researcher.search(plan)
        self.fetcher.fetch.assert_called_once_with(new_url)

    def test_expired_wrong_version_and_future_seed_are_not_used_as_fresh_evidence(self):
        for change in ({'fetched_at': datetime.now(timezone.utc) - timedelta(days=2)},
                       {'extractor_version': 0},
                       {'fetched_at': datetime.now(timezone.utc) + timedelta(days=1)}):
            with self.subTest(change=change):
                self.documents[self.seed] = dict(self.record(), **change)
                self.fetcher.fetch.reset_mock()
                self.fetcher.fetch.side_effect = SourceError('source_network_failed')
                matches, errors = self.researcher.search(self.plan)
                self.assertEqual(matches, [])
                self.assertEqual(errors, ['source_network_failed'])
                self.fetcher.fetch.assert_called_once_with(self.seed)

    def test_seed_cache_is_scoped_to_selected_topics(self):
        self.documents[self.seed]['links'] = []
        self.documents[self.tax_seed] = self.record()
        self.matches = [{'url': self.tax_seed, 'text': 'Документы для регистрации перечислены в инструкции.'}]
        matches, _ = self.researcher.search(self.plan)
        self.assertEqual(matches, [])
        for call in self.store.search_documents.call_args_list:
            self.assertNotIn(self.tax_seed, call.args[1])

    def test_two_topics_require_seed_evidence_for_both_before_shortcut(self):
        self.documents[self.tax_seed] = self.record()
        self.settings = Settings(sources_file=self.settings.sources_file, max_pages=3)
        researcher = Researcher(self.store, self.settings, fetcher=self.fetcher)
        researcher.search(self.plan.model_copy(update={'topics': ['registration', 'taxes']}))
        self.fetcher.fetch.assert_called_once_with(self.linked)

    def test_repeated_complete_query_reuses_crawl_but_new_condition_does_not(self):
        plan = self.plan.model_copy(update={'query': 'Я совершеннолетний гражданин РФ. Какие документы нужны для регистрации ИП?'})
        self.researcher.search(plan)
        self.fetcher.fetch.assert_called_once_with(self.linked)
        new_url = 'https://nalog.gov.ru/minor'
        self.documents[self.seed]['links'].insert(0, {'url': new_url, 'title': 'Документы регистрации несовершеннолетнего'})
        self.fetcher.fetch.reset_mock()
        matches, errors = self.researcher.search(plan)
        self.assertEqual(matches, self.matches)
        self.assertEqual(errors, [])
        self.fetcher.fetch.assert_not_called()
        self.researcher.search(plan.model_copy(update={'query': 'Документы регистрации несовершеннолетнего'}))
        self.fetcher.fetch.assert_called_once_with(new_url)

    def test_memo_rechecks_every_document_version_and_expiry(self):
        plan = self.plan.model_copy(update={'query': 'Гражданин РФ документы регистрации ИП'})
        for change in ({'fetched_at': datetime.now(timezone.utc) - timedelta(days=2)}, {'extractor_version': 0}):
            with self.subTest(change=change):
                self.documents.pop(self.linked, None)
                researcher = Researcher(self.store, self.settings, fetcher=self.fetcher)
                researcher.search(plan)
                self.documents[self.linked].update(change)
                self.fetcher.fetch.reset_mock()
                researcher.search(plan)
                self.fetcher.fetch.assert_called_once_with(self.linked)

    def test_failed_crawl_is_retried_instead_of_memoized(self):
        plan = self.plan.model_copy(update={'query': 'Гражданин РФ документы регистрации ИП'})
        self.fetcher.fetch.side_effect = SourceError('source_network_failed')
        self.researcher.search(plan)
        self.researcher.search(plan)
        self.assertEqual(self.fetcher.fetch.call_count, 2)

    def test_memo_does_not_cross_topic_boundary(self):
        plan = self.plan.model_copy(update={'query': 'Гражданин РФ документы регистрации ИП'})
        self.researcher.search(plan)
        self.documents[self.tax_seed] = self.record()
        matches, _ = self.researcher.search(plan.model_copy(update={'topics': ['taxes']}))
        self.assertEqual(matches, [])

    def test_memo_rechecks_allowlist_before_returning_cached_sources(self):
        plan = self.plan.model_copy(update={'query': 'Гражданин РФ документы регистрации ИП'})
        self.researcher.search(plan)
        self.researcher.catalog.policy = URLPolicy(['other.gov.ru'])
        self.fetcher.fetch.reset_mock()
        matches, errors = self.researcher.search(plan)
        self.assertEqual(matches, [])
        self.assertEqual(errors, ['url_not_allowed'])
        self.fetcher.fetch.assert_not_called()

    def test_completed_search_memo_expires_even_if_documents_are_still_fresh(self):
        plan = self.plan.model_copy(update={'query': 'Гражданин РФ документы регистрации ИП'})
        with patch('ip_bot.sources.time.monotonic', return_value=0):
            self.researcher.search(plan)
        new_url = 'https://nalog.gov.ru/new-details'
        self.documents[self.seed]['links'].insert(0, {'url': new_url, 'title': 'Гражданин РФ документы регистрации ИП'})
        self.fetcher.fetch.reset_mock()
        with patch('ip_bot.sources.time.monotonic', return_value=self.settings.source_ttl_hours * 3600 + 1):
            self.researcher.search(plan)
        self.fetcher.fetch.assert_called_once_with(new_url)
