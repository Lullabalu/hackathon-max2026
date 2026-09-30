"""Ограниченный обход официальных сайтов без платного поискового API."""

from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .budget import check_budget, remaining_seconds, request_timeout


EXTRACTOR_VERSION = 3


class SourceError(RuntimeError):
    pass


STOP_WORDS = set('какие какой нужно нужны нужен есть это для как что или при про мне хочу можно будет чтобы где когда узнать'.split())


def words(text):
    return {word for word in re.findall(r'[a-zа-яё0-9]{3,}', text.casefold().replace('ё', 'е')) if word not in STOP_WORDS}


def relevance(text, query):
    tokens = words(text)
    return sum(any(token.startswith(word[:5]) for token in tokens) for word in words(query))


class URLPolicy:
    def __init__(self, domains):
        self.domains = tuple(domain.lower().strip('.') for domain in domains)
        if not self.domains or any('/' in item or ':' in item or not item for item in self.domains):
            raise ValueError('Некорректный список allowed_domains.')

    def normalize(self, url):
        try:
            if not isinstance(url, str) or len(url) > 1800 or any(ord(c) < 33 for c in url):
                raise ValueError
            parsed = urlsplit(url)
            host = (parsed.hostname or '').encode('idna').decode('ascii').lower()
            if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                    or parsed.port not in {None, 443} or not host
                    or not any(host == domain or host.endswith('.' + domain) for domain in self.domains)):
                raise ValueError
        except (ValueError, UnicodeError):
            raise SourceError('url_not_allowed') from None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SourceError('ip_literal_not_allowed')
        path = parsed.path or '/'
        if any(ord(c) > 127 for c in path + parsed.query):
            from urllib.parse import quote
            path = quote(path, safe='/%:@-._~!$&\'()*+,;=')
            query = quote(parsed.query, safe='=&%:@/?-._~!$\'()*+,;')
        else:
            query = parsed.query
        return urlunsplit(('https', host, path, query, ''))

    def addresses(self, host):
        check_budget()
        try:
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except OSError:
            raise SourceError('source_dns_failed') from None
        check_budget()
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SourceError('non_public_address')
        return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    """TLS проверяет hostname, TCP подключается к уже проверенному IP."""
    def __init__(self, host, addresses, context):
        super().__init__(host, timeout=request_timeout(10), context=context)
        self.addresses = addresses

    def connect(self):
        last_error = None
        for address in self.addresses[:2]:
            raw = None
            try:
                check_budget()
                raw = socket.create_connection((address, 443), request_timeout(10))
                check_budget()
                raw.settimeout(request_timeout(10))
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                raw = None
                return
            except OSError as error:
                last_error = error
            finally:
                # BudgetExpired наследует BaseException и должен выйти наружу;
                # незавершённое TLS-соединение при этом всё равно закрываем.
                if raw is not None:
                    raw.close()
        raise last_error or OSError('connection failed')


class SafeFetcher:
    agent = 'MaxIPBot/1.0'

    def __init__(self, policy):
        self.policy = policy
        self.context = ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE'))
        self.last_request = {}
        self.robots = {}
        self.delays = {}

    def raw(self, url, *, limit=6_000_000, obey_robots=False):
        for _ in range(4):
            check_budget()
            url = self.policy.normalize(url)
            if obey_robots:
                self.ensure_allowed(url)
            parsed = urlsplit(url)
            addresses = self.policy.addresses(parsed.hostname)
            delay = self.delays.get(parsed.hostname, 1)
            wait = max(0, delay - (time.monotonic() - self.last_request.get(parsed.hostname, 0)))
            remaining = remaining_seconds()
            if remaining is not None and wait >= max(0, remaining - 1):
                raise SourceError('search_budget_exhausted')
            time.sleep(wait)
            check_budget()
            self.last_request[parsed.hostname] = time.monotonic()
            connection = PinnedHTTPS(parsed.hostname, addresses, self.context)
            response = None
            try:
                target = parsed.path + ('?' + parsed.query if parsed.query else '')
                check_budget()
                connection.request('GET', target, headers={'User-Agent': self.agent, 'Accept-Encoding': 'identity'})
                check_budget()
                if connection.sock is not None:
                    connection.sock.settimeout(request_timeout(10))
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader('Location')
                    if not location:
                        raise SourceError('invalid_redirect')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise SourceError(f'source_http_{response.status}')
                length = response.getheader('Content-Length')
                if length and int(length) > limit:
                    raise SourceError('source_too_large')
                if response.getheader('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
                    raise SourceError('unsupported_encoding')
                data = bytearray()
                started = time.monotonic()
                while True:
                    check_budget()
                    if connection.sock is not None:
                        connection.sock.settimeout(request_timeout(10))
                    chunk = response.read(min(65536, limit + 1 - len(data)))
                    check_budget()
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit or time.monotonic() - started > 30:
                        raise SourceError('source_limit_exceeded')
                return url, response.getheader('Content-Type', ''), bytes(data)
            except (OSError, http.client.HTTPException, ValueError):
                raise SourceError('source_network_failed') from None
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
        raise SourceError('too_many_redirects')

    def ensure_allowed(self, url):
        check_budget()
        url = self.policy.normalize(url)
        parsed = urlsplit(url)
        origin = f'https://{parsed.hostname}'
        cached = self.robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > 3600:
            rules = RobotFileParser()
            try:
                _, _, data = self.raw(origin + '/robots.txt', limit=128_000)
                check_budget()
                rules.parse(data.decode('utf-8', errors='replace').splitlines())
            except SourceError as error:
                if str(error) == 'search_budget_exhausted':
                    raise
                if str(error) == 'source_http_404':
                    rules.parse(['User-agent: *', 'Allow: /'])
                else:
                    raise SourceError('robots_unavailable') from None
            self.robots[origin] = time.monotonic(), rules
        else:
            rules = cached[1]
        if not rules.can_fetch(self.agent, url):
            raise SourceError('robots_disallowed')
        delay = rules.crawl_delay(self.agent) or 1
        if delay > 10:
            raise SourceError('crawl_delay_too_long')
        self.delays[parsed.hostname] = max(1, delay)

    def fetch(self, url):
        # Проверяем robots до каждого запроса, включая новую цель перенаправления.
        return self.raw(url, obey_robots=True)


@dataclass
class Document:
    url: str
    title: str
    text: str
    links: list
    content_type: str


def extract_document(url, content_type, raw):
    check_budget()
    kind = content_type.split(';')[0].lower()
    suffix = urlsplit(url).path.lower()
    links = []
    title = urlsplit(url).path.rsplit('/', 1)[-1] or urlsplit(url).hostname
    try:
        if kind == 'application/pdf' or suffix.endswith('.pdf'):
            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                raise SourceError('encrypted_pdf')
            pages = []
            for page in reader.pages[:25]:
                check_budget()
                pages.append((page.extract_text() or '')[:16000])
            text = '\n'.join(pages)
            if reader.metadata and reader.metadata.title:
                title = str(reader.metadata.title)[:250]
        elif suffix.endswith('.docx') or kind == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                with archive.open('word/document.xml') as file:
                    xml = file.read(2_000_001)
            if len(xml) > 2_000_000 or b'<!DOCTYPE' in xml.upper():
                raise SourceError('document_too_large')
            check_budget()
            root = ET.fromstring(xml)
            check_budget()
            text = ' '.join(node.text or '' for node in root.iter() if node.tag.endswith('}t'))
        elif kind in {'text/html', 'application/xhtml+xml', ''}:
            soup = BeautifulSoup(raw, 'html.parser')
            check_budget()
            if soup.title:
                title = soup.title.get_text(' ', strip=True)
            # .content часто используется для модального окна выбора региона.
            # ASP.NET помещает всю страницу в form: удаляем поля, но не оболочку.
            main = soup.find('main') or soup.find('article') or soup.select_one('#content, .wrap-content') or soup.body or soup
            for tag in main.select('script, style, nav, header, footer, noscript, svg, input, select, button, textarea, [role="dialog"]'):
                check_budget()
                tag.decompose()
            for link in main.find_all('a', href=True):
                check_budget()
                target = urljoin(url, link['href'])
                caption = link.get_text(' ', strip=True)[:250]
                if caption:
                    links.append({'url': target, 'title': caption})
            # Современные страницы ФНС помещают статью между колонками меню.
            # .wrap-content также содержит телефон, хлебные крошки и сервисы.
            modern_article = main.select_one('#divSecondPageColumns .page-content__center')
            # Старые страницы ФНС create_business используют таблицы вместо main:
            # текст статьи и заголовок шага находятся в .box-r/.t_section-text.
            # Ссылки уже собраны из всей области страницы, включая формы в sidebar.
            article_text = main.select_one('.t_section-text')
            if modern_article is not None:
                main = modern_article
                for tag in main.select('.content_list'):
                    tag.decompose()
                # .note — существенные условия статьи, включая исключения.
            elif article_text is not None:
                parent = article_text.parent
                main = parent if 'box-r' in parent.get('class', []) else article_text
            # Названия соседних документов и пунктов меню не являются текстом статьи.
            for tag in main.select('aside, [role="complementary"], .sidebar, .column-left, .column-right, .bottom_nav, .pagination'):
                tag.decompose()
            text = main.get_text('\n', strip=True)
        elif kind == 'text/plain':
            text = raw.decode('utf-8', errors='replace')
        else:
            raise SourceError('unsupported_document')
    except SourceError:
        raise
    except Exception:
        raise SourceError('document_parse_failed') from None
    check_budget()
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()[:100_000]
    if len(text) < 60:
        raise SourceError('document_has_no_text')
    check_budget()
    return Document(url, title[:250], text, links[:400], kind)


def chunks(text, size=1000, overlap=160):
    for start in range(0, len(text), size - overlap):
        check_budget()
        part = text[start:start + size].strip()
        if len(part) >= 40:
            yield part


class Catalog:
    def __init__(self, filename):
        data = json.loads(Path(filename).read_text())
        self.policy = URLPolicy(data['allowed_domains'])
        self.sources = {item['id']: item for item in data['sources']}
        if not self.sources:
            raise ValueError('Каталог источников пуст.')
        for source in self.sources.values():
            source['urls'] = [self.policy.normalize(url) for url in source['urls']]

    def description(self):
        return [{'id': item['id'], 'title': item['title']} for item in self.sources.values()]


class Researcher:
    def __init__(self, store, settings, catalog=None, fetcher=None):
        self.store = store
        self.settings = settings
        self.catalog = catalog or Catalog(settings.sources_file)
        self.fetcher = fetcher or SafeFetcher(self.catalog.policy)
        self.completed_searches = {}

    def fresh(self, document):
        if not document or document.get('extractor_version') != EXTRACTOR_VERSION:
            return False
        age = (datetime.now(timezone.utc) - document['fetched_at']).total_seconds()
        return 0 <= age < self.settings.source_ttl_hours * 3600

    def fresh_urls(self, urls):
        result = []
        for url in urls:
            check_budget()
            try:
                normalized = self.catalog.policy.normalize(url)
            except SourceError:
                continue
            if normalized == url and self.fresh(self.store.document(url)):
                result.append(url)
        return result

    @staticmethod
    def cache_terms(text):
        # Короткие основы допускают русские окончания; номера форм и годы точные.
        tokens = words(text)
        tokens.update(value.replace(',', '.') for value in re.findall(r'(?<!\w)\d+(?:[.,]\d+)?(?!\w)', text))
        return {word if any(char.isdigit() for char in word) else word[:5] for word in tokens}

    def has_cache_evidence(self, matches, terms):
        identifiers = {term for term in terms if any(char.isdigit() for char in term)}
        return any(len(terms & self.cache_terms(item['text'])) >= 2
                   and identifiers <= self.cache_terms(item['text']) for item in matches)

    def cached_matches(self, plan, query, topics, memo_key):
        check_budget()
        terms = self.cache_terms(plan.query)
        if len(terms) < 2:
            return None
        previous = self.completed_searches.get(memo_key)
        if previous:
            urls, completed_at = previous
            if (time.monotonic() - completed_at < self.settings.source_ttl_hours * 3600
                    and self.fresh_urls(urls) == urls):
                matches = self.store.search_documents(query, urls, limit=8)
                check_budget()
                if self.has_cache_evidence(matches, terms):
                    return matches
            self.completed_searches.pop(memo_key, None)
        seeds = list(dict.fromkeys(url for key in topics for url in self.catalog.sources[key]['urls']))[:self.settings.max_pages]
        fresh = self.fresh_urls(seeds)
        if not fresh:
            return None
        matches = self.store.search_documents(query, fresh, limit=8)
        check_budget()
        qualified_urls = {item['url'] for item in matches
                          if item['url'] in fresh and terms <= self.cache_terms(item['text'])}
        # Квалифицируем документ по подходящему фрагменту, но сохраняем другие
        # найденные части той же статьи: в них могут быть оговорки/исключения.
        matches = [item for item in matches if item['url'] in qualified_urls]
        # Для нескольких тем нельзя принять совпадение только по одной из них.
        if matches and all(any(item['url'] in self.catalog.sources[key]['urls'] for item in matches) for key in topics):
            return matches
        return None

    def search(self, plan):
        check_budget()
        topics = list(dict.fromkeys(key for key in plan.topics if key in self.catalog.sources))[:2]
        query = ' '.join([plan.query, *plan.keywords])[:800]
        if not topics:
            topics = sorted(self.catalog.sources, key=lambda key: relevance(self.catalog.sources[key]['title'], query), reverse=True)[:1]
        normalize = lambda value: ' '.join(value.casefold().replace('ё', 'е').split())
        memo_key = (tuple(sorted(topics)), normalize(plan.query), tuple(sorted(normalize(value) for value in plan.keywords)), normalize(query))
        cached = self.cached_matches(plan, query, topics, memo_key)
        if cached:
            return cached, []
        queue, sequence, seen, usable, errors = [], 0, set(), [], []
        for key in topics:
            for url in self.catalog.sources[key]['urls']:
                heapq.heappush(queue, (-100, sequence, url, 0))
                sequence += 1
        while queue and len(seen) < self.settings.max_pages:
            check_budget()
            remaining = remaining_seconds()
            if remaining is not None and remaining < 1:
                errors.append('search_budget_exhausted')
                break
            _, _, url, depth = heapq.heappop(queue)
            try:
                url = self.catalog.policy.normalize(url)
                if url in seen:
                    continue
                seen.add(url)
                cached = self.store.document(url)
                check_budget()
                if self.fresh(cached):
                    links = cached['links']
                    final_url = url
                else:
                    check_budget()
                    final_url, kind, raw = self.fetcher.fetch(url)
                    check_budget()
                    document = extract_document(final_url, kind, raw)
                    check_budget()
                    self.store.save_document(document)
                    links = document.links
                usable.append(final_url)
                if depth < 2:
                    for link in links:
                        check_budget()
                        try:
                            target = self.catalog.policy.normalize(link['url'])
                        except SourceError:
                            continue
                        score = relevance(link['title'] + ' ' + target, query)
                        if score and target not in seen and not re.search(r'\.(jpg|png|zip|exe|doc|xls|xlsx)$', urlsplit(target).path, re.I):
                            heapq.heappush(queue, (-score, sequence, target, depth + 1))
                            sequence += 1
            except SourceError as error:
                errors.append(str(error))
                if str(error) == 'search_budget_exhausted':
                    break
        usable = list(dict.fromkeys(usable))
        check_budget()
        matches = self.store.search_documents(query, usable, limit=8)
        check_budget()
        terms = self.cache_terms(plan.query)
        # Запоминаем завершённый ограниченный обход только этого запроса, не всей
        # темы: новый вопрос/условие/набор ключевых слов снова требует поиска.
        if not errors and self.has_cache_evidence(matches, terms):
            self.completed_searches[memo_key] = (usable, time.monotonic())
            if len(self.completed_searches) > 64:
                self.completed_searches.pop(next(iter(self.completed_searches)))
        return matches, errors
