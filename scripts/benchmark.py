"""Измерение локального ответа: python -m scripts.benchmark [--skip-model].

Нужна DEMO_DATABASE_URL, загруженная в окружение. Создаёт временную схему;
без --skip-model читает реальные источники ФНС и вызывает локальную Ollama
для краткой выжимки. С --skip-model проверяет только разговор и ответы по анкете.
Ничего не отправляет в MAX. Отчёт: data/performance-check.json.
"""

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
import time
from unittest.mock import patch
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from ip_bot.__main__ import drain
from ip_bot.assistant import Assistant
from ip_bot.budget import PROCESSING_SECONDS
from ip_bot.llm import Ollama
from ip_bot.settings import Settings
from ip_bot.sources import PinnedHTTPS, Researcher
from ip_bot.storage import Store


QUESTION = 'Как заполнить Р21001?'
FEE_QUESTION = 'Нужно ли платить госпошлину при регистрации ИП через МФЦ?'
PROFILE = {
    'answers': {'activity': 'Разработка сайтов', 'region': 'Казань', 'revenue': '1200000',
                'expenses': '200000', 'employees': 0, 'clients': 'Физлица'},
    'tax_options': {'new_ip': True, 'previous_revenue': '0', 'npd_activity_eligible': True,
                    'npd_deduction_remaining': '5000'},
    'registration': {'citizenship': 'ru', 'age': 31, 'method': 'mfc'},
    'selected_regime': 'npd', 'consent': {'accepted': True}, 'paused': True,
}
REPORT_PATH = Path(__file__).resolve().parent.parent / 'data/performance-check.json'


class TimedModel(Ollama):
    def __init__(self, settings, *, disabled=False):
        super().__init__(settings)
        self.disabled = disabled
        self.stages = []

    def generate(self, schema, *args, **kwargs):
        stage = {'schema': schema.__name__}
        # Не записываем prompt целиком: для тестовой анкеты достаточно признака,
        # что актуальный регион попал в контекст нейронной выжимки.
        if schema.__name__ == 'Summary' and args and isinstance(args[-1], str):
            try:
                context = json.loads(args[-1])
                stage['profile_seen'] = context.get('profile', {}).get('region') == PROFILE['answers']['region']
                stage['input'] = context
            except (ValueError, AttributeError):
                stage['profile_seen'] = False
        started = time.perf_counter()
        try:
            if self.disabled:
                raise AssertionError('При --skip-model быстрый сценарий не должен вызывать модель')
            result = super().generate(schema, *args, **kwargs)
            stage['success'] = True
            stage['result'] = result.model_dump()
            if schema.__name__ == 'Summary':
                (REPORT_PATH.parent / 'quality-last-summary.json').write_text(
                    json.dumps(stage, ensure_ascii=False, indent=2), encoding='utf-8')
            return result
        except Exception as error:
            stage.update(success=False, error_type=type(error).__name__)
            raise
        finally:
            stage['seconds'] = round(time.perf_counter() - started, 6)
            self.stages.append(stage)


class TimedResearcher(Researcher):
    def __init__(self, *args, disabled=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.disabled = disabled
        self.stages = []
        self.returned_passages = []

    def search(self, plan):
        started = time.perf_counter()
        stage = {}
        try:
            if self.disabled:
                raise AssertionError('При --skip-model быстрый сценарий не должен искать источники')
            passages, failures = super().search(plan)
            self.returned_passages.extend(passages)
            stage.update(passages=len(passages), failures=failures)
            return passages, failures
        finally:
            stage['seconds'] = round(time.perf_counter() - started, 6)
            self.stages.append(stage)


def check(case, condition, description):
    case['checks'].append({'description': description, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(description)


def answer_metrics(reply, passages=()):
    """Measure visible output, not the model's hidden evidence quotes."""
    normalized = ' '.join(reply.split()).casefold()
    copied = max((SequenceMatcher(None, normalized, ' '.join(item['text'].split()).casefold(),
                                  autojunk=False).find_longest_match().size
                  for item in passages), default=0)
    quotes = re.findall(r'«([^»]*)»|"([^"\n]*)"', reply)
    quoted_lengths = [len(first or second) for first, second in quotes]
    quoted_lengths.extend(len(line.lstrip('> ').strip()) for line in reply.splitlines()
                          if line.lstrip().startswith('>'))
    return {'reply_characters': len(reply), 'reply_words': len(reply.split()),
            'longest_quote_characters': max(quoted_lengths, default=0),
            'longest_source_copy_characters': copied}


def check_summary_case(case, *, expected_model_calls=None, allow_clarification=False):
    """A fast timeout/fallback must not masquerade as successful summarization."""
    summaries = [stage for stage in case['model_stages'] if stage['schema'] == 'Summary']
    check(case, bool(summaries) and any(stage.get('success') for stage in summaries),
          'Модель успешно подготовила выжимку')
    summary = next(stage for stage in reversed(summaries) if stage.get('success'))
    result = summary.get('result', {})
    points, clarification = result.get('points', []), result.get('clarification', '')
    check(case, 1 <= len(points) <= 6 or (allow_clarification and not points and bool(clarification)),
          'Есть краткие пункты ответа либо честное уточнение при нехватке сведений')
    if expected_model_calls is None:
        check(case, 1 <= case['model_calls'] <= 6, 'Число вызовов укладывается в разбор, поиск и повторную проверку')
    else:
        check(case, expected_model_calls <= case['model_calls'] <= 6, 'Модель подготовила ответ с допустимыми повторными проверками')
    check(case, summary.get('profile_seen'), 'Сохранённый регион присутствует в контексте модели')
    check(case, case['reply_characters'] <= 3600, 'Ответ не длиннее 3600 символов вместе со ссылками')
    check(case, case['longest_quote_characters'] <= 360 and case['longest_source_copy_characters'] <= 360,
          'В ответе нет больших цитат или скопированных абзацев')
    reply = ' '.join(case['reply'].casefold().split())
    check(case, not any(marker in reply for marker in (
        'не успел', 'не удалось составить', 'не удалось подготовить', 'модель сейчас недоступна',
        'выдержки из официальных', 'не удалось получить выжимку',
    )), 'Выжимка не подменена сообщением о сбое или длинными выдержками')
    if points:
        check(case, any(' '.join(point.get('text', '').casefold().split()) in reply
                        for point in points if point.get('text')),
              'Пользователь получил пункт выжимки, а не запасной ответ со ссылками')
        check(case, bool(re.search(r'\[\d+\]', reply)) and 'https://' in reply,
              'Выжимка содержит проверяемую ссылку на официальный источник')


def check_fee_answer(case):
    reply = ' '.join(case['reply'].casefold().split())
    check(case, 'электрон' in reply, 'Сохранено условие электронного направления документов')
    check(case, bool(re.search(r'(?:не\s+(?:нужно|надо|требуется|нужна|взимается|уплачивается|платится|платите)|'
                              r'без\s+(?:уплаты\s+)?(?:госпошлин|пошлин)|освобожд)', reply)),
          'Есть прямой ответ о том, что пошлину при указанном условии платить не нужно')


def check_registration_answer(case, *, followup=False, require_filling=False):
    """Regression checks for the synthetic MFC profile, not full entailment.

    Inspect the visible answer, excluding source titles/URLs. A region in the
    prompt is not proof that the model respected the user's filing method.
    """
    body = re.split(r'\bисточники\s*:', case['reply'], maxsplit=1, flags=re.I)[0]
    body = re.sub(r'\[\d+\]', '', body.casefold().replace('ё', 'е'))
    body = re.sub(r'многофункциональн[а-я]*\s+центр[а-я]*', 'мфц', body)
    clauses = [' '.join(part.split()) for part in re.split(r'(?<=[.!?;])\s+|\n+|,\s*(?:но|однако|зато)\s+', body) if part.strip()]
    mfc = bool(re.search(r'\bмфц\b', body))
    signature = r'\b(?:укэп|кэп|уэп|эцп|эп)\b|электронн[а-я]*\s+(?:квалифицированн[а-я]*\s+)?подпис[а-я]*'
    optional = r'\b(?:можно|можете|необязательн[а-я]*|добровольн[а-я]*)\b|при желании|как альтернатив'
    not_required = r'\bне\s+(?:нуж[а-я]*|требу[а-я]*|обязател[а-я]*|надо|понадоб[а-я]*)\b'
    online = (r'\bонлайн\b|через интернет|электронн[а-я]*\s+(?:вид[а-я]*|форм[а-я]*|подач[а-я]*)|'
              r'через\s+(?:сайт|сервис|портал)[а-я]*|на\s+(?:сайт|портал)[а-я]*')
    electronic_condition = (r'(?:для|при)\s+электронн[а-я]*\s+подач[а-я]*|'
                            r'если\b.{0,80}(?:онлайн|электронн[а-я]*|через\s+(?:интернет|сервис|портал))')
    filing = r'\b(?:подать|пода[а-я]*|направ[а-я]*|отправ[а-я]*|переда[а-я]*)\b'
    # A fee clause such as "госпошлина не требуется при направлении ..."
    # is not an instruction to change the filing route. Require an imperative
    # or a modal attached to the filing action itself.
    filing_infinitive = r'(?:подать|подавать|направить|направлять|отправить|отправлять|передать|передавать)'
    directive = (
        r'\b(?:подайте|подавайте|отправьте|отправляйте|направьте|передайте|перейдите)\b|'
        r'\b(?:нужно|надо|необходимо|обязательно|требуется|должен|должна|должны|придется)\s+'
        r'(?:(?:вам|самостоятельно|лично|электронно|сначала|обязательно|будет|именно|только|теперь)\s+){0,3}'
        + filing_infinitive + r'\b|'
        r'\b(?:обязательна|требуется|необходима)\s+(?:именно\s+)?(?:подача|отправка|передача)\b|'
        r'\b(?:подача|подается|подаются|подают|направляются|направляется|отправляется|отправляются|'
        r'подать|подавать|отправить|отправлять)\s+(?:[а-я]+\s+){0,4}(?:только|исключительно)\b')
    incompatible_signature = []
    incompatible_route = []
    for clause in clauses:
        choice = bool(re.search(optional, clause))
        for mention in re.finditer(signature, clause):
            before, after = clause[:mention.start()].rsplit(',', 1)[-1], clause[mention.end():].split(',', 1)[0]
            negative_signature = bool(
                re.search(not_required, after[:50]) or
                re.search(not_required + r'\s+(?:(?:получать|оформлять|использовать|применять)\s+)?'
                          r'(?:(?:усиленн[а-я]*|квалифицированн[а-я]*)\s+)*$', before) or
                re.search(r'\bбез\s+(?:(?:использования|оформления|получения)\s+)?$', before))
            # A condition for a different route is relevant only when the
            # answer also preserves the route the user actually selected.
            conditional = bool(re.search(electronic_condition, clause))
            if not (negative_signature or mfc and (choice or conditional)):
                incompatible_signature.append(clause)
        mfc_sender = bool(re.search(r'\bмфц\s+(?:сам\s+)?(?:направ[а-я]*|отправ[а-я]*|переда[а-я]*)\b', clause))
        alternative = bool(re.search(online, clause) or re.search(r'\bпочт[а-я]*\b|лично\s+в\s+(?:фнс|налогов[а-я]*|инспекц[а-я]*)', clause))
        if alternative and re.search(filing, clause) and re.search(directive, clause):
            negative_route = bool(re.search(r'\bне\s+(?:(?:нужно|надо|обязательно)\s+)?'
                                            r'(?:подать|подава[а-я]*|отправ[а-я]*|направ[а-я]*)\b', clause))
            if not (negative_route or choice or mfc_sender or ('или' in clause.split() and 'мфц' in clause)):
                incompatible_route.append(clause)
        if re.search(r'\bвместо\s+(?:подачи\s+через\s+)?мфц\b|\bмфц\s+не\s+принима[а-я]*', clause):
            incompatible_route.append(clause)
    check(case, not incompatible_signature,
          'Для сохранённой подачи через МФЦ не навязана обязательная электронная подпись')
    check(case, not incompatible_route,
          'Ответ не требует заменить выбранную подачу через МФЦ другим способом')

    if require_filling:
        clarifies = any(stage.get('schema') == 'Summary' and
                        not stage.get('result', {}).get('points') and stage.get('result', {}).get('clarification')
                        for stage in case.get('model_stages', []))
        case['filling_answer'] = 'clarification' if clarifies else 'instruction'
        if not clarifies:
            concrete = bool(re.search(
                r'\b(?:распечат[а-я]*|бумаг[а-я]*|печатн[а-я]*|заглавн[а-я]*|чернил[а-я]*|клет[а-я]*|'
                r'строк[а-я]*|раздел[а-я]*|лист[а-я]*|фамили[а-я]*|имя|отчеств[а-я]*|фио|адрес[а-я]*|'
                r'гражданств[а-я]*|оквэд|сервис[а-я]*|программ[а-я]*|редактор[а-я]*|компьютер[а-я]*|'
                r'конструктор[а-я]*|поля|поле|полей|pdf)\b|от руки|вручную', body))
            electronic_filling = bool(re.search(r'\b(?:заполн[а-я]*|сформир[а-я]*|оформ[а-я]*)\b', body)
                                      and re.search(r'\b(?:онлайн|электронн[а-я]*)\b', body))
            check(case, concrete or electronic_filling,
                  'Ответ объясняет способ заполнения или конкретные поля, а не повторяет «заполните Р21001»')

    if followup:
        destination = (r'\b(?:в|через)\s+(?:[а-я]+\s+){0,3}мфц\b|\bмфц\s+принима[а-я]*|'
                       r'место\s+подачи\s*[:—–-]?\s*мфц\b')
        action = (r'\b(?:подать|подавать|подайте|подавайте|подается|подаются|подают|передать|передавать|передайте|'
                  r'направить|направлять|направьте|отправить|отправлять|отправьте|отнесите|принесите|'
                  r'обратитесь|принима[а-я]*)\b|место\s+подачи')
        direct_destination = any(re.search(destination, clause) and re.search(action, clause)
                                 and not re.search(r'\b(?:не|нельзя)\s+(?:(?:нужно|надо|следует)\s+)?'
                                                   r'(?:подать|подава[а-я]*|подайте|переда[а-я]*|направ[а-я]*|отправ[а-я]*|принима[а-я]*)|'
                                                   r'\bне\s+(?:в|через)\s+мфц\b', clause)
                                 for clause in clauses)
        check(case, direct_destination,
              'Уточнение отвечает куда подать заявление с учётом выбранного МФЦ')


def benchmark(settings, *, skip_model, report):
    schema = 'performance_' + uuid.uuid4().hex
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        store = None
        try:
            # Схема изолирована: общий lock основного worker здесь не нужен.
            store = Store(make_conninfo(settings.database_url, options=f'-c search_path={schema}'), worker=False)
            model = TimedModel(settings, disabled=skip_model)
            researcher = TimedResearcher(store, settings, disabled=skip_model)
            assistant = Assistant(store, settings, model=model, researcher=researcher)
            store.receive([{'event_id': 'consent', 'user_id': 1, 'text': '/consent'}])
            drain(store, lambda *_: None, assistant)
            store.receive([{'event_id': 'free-chat', 'user_id': 1, 'text': '/chat'}])
            drain(store, lambda *_: None, assistant)
            network_requests = 0
            original_request = PinnedHTTPS.request

            def counted_request(connection, *args, **kwargs):
                nonlocal network_requests
                network_requests += 1
                return original_request(connection, *args, **kwargs)

            def run_case(name, question):
                model_start = len(model.stages)
                research_start = len(researcher.stages)
                passages_start = len(researcher.returned_passages)
                requests_start = network_requests
                messages = []
                case = {'name': name, 'question': question, 'checks': []}
                report['cases'].append(case)
                started = time.perf_counter()
                try:
                    store.receive([{'event_id': name, 'user_id': 1, 'text': question}])
                    drain(store, lambda user_id, text: messages.append(text), assistant)
                finally:
                    model_stages = model.stages[model_start:]
                    research_stages = researcher.stages[research_start:]
                    case.update(
                        seconds=round(time.perf_counter() - started, 6),
                        model_calls=len(model_stages),
                        model_seconds=round(sum(stage['seconds'] for stage in model_stages), 6),
                        model_stages=model_stages,
                        research_calls=len(research_stages),
                        research_seconds=round(sum(stage['seconds'] for stage in research_stages), 6),
                        research_stages=research_stages,
                        source_http_requests=network_requests - requests_start,
                        messages=len(messages),
                        reply=messages[-1] if messages else '',
                    )
                    case.update(answer_metrics(case['reply'], researcher.returned_passages[passages_start:]))
                    print(
                        f'{name}: {case["seconds"]:.3f} сек.; '
                        f'модель: {case["model_calls"]}; HTTP источников: {case["source_http_requests"]}; '
                        f'ответ: {case["reply_characters"]} знаков',
                        flush=True,
                    )
                check(case, store.pending() is None, 'Событие полностью обработано')
                check(case, bool(case['reply']), 'Ответ не пустой')
                return case

            # Учитываем каждый запрос к источникам, включая robots.txt,
            # перенаправления и неудачные попытки; запросы Ollama считаются отдельно.
            with patch.object(PinnedHTTPS, 'request', counted_request):
                for name, question in [('about', 'Бро, кто ты такой?'), ('thanks', 'Спасибо большое!')]:
                    case = run_case(name, question)
                    check(case, case['model_calls'] == 0, 'Разговорная реплика без модели')
                    check(case, case['research_calls'] == 0, 'Разговорная реплика без поиска')
                    check(case, case['source_http_requests'] == 0, 'Разговорная реплика без интернета')
                    check(case, case['messages'] == 1, 'Разговорная реплика без уведомления о поиске')

                # Только синтетические данные внутри временной схемы. Проверка
                # хранит и читает профиль через тот же PostgreSQL, что и worker.
                store.db.execute('UPDATE profiles SET state = %s WHERE user_id = 1', (Jsonb(PROFILE),))
                for name, question in [('profile_memory', 'Какой регион я указал?'),
                                       ('personal_tax', 'Сколько мне платить налогов?')]:
                    case = run_case(name, question)
                    check(case, case['model_calls'] == 0, 'Известные данные анкеты и расчёт без модели')
                    check(case, case['research_calls'] == 0 and case['source_http_requests'] == 0,
                          'Ответ по известной анкете не требует поиска')
                    check(case, 'Казань' in case['reply'], 'Ответ опирается на сохранённый регион')
                state = store.load_profile(1)
                check(case, all(state.get(key) == value for key, value in PROFILE.items()),
                      'Анкета сохранена без подмены исходных данных')

                if skip_model:
                    report['skipped'] = [f'{name}: --skip-model отключает поиск с нейронной выжимкой'
                                         for name in ('registration_first', 'registration_cached', 'followup', 'registration_fee')]
                else:
                    first = run_case('registration_first', QUESTION)
                    check_summary_case(first, expected_model_calls=1, allow_clarification=True)
                    check_registration_answer(first, require_filling=True)
                    check(first, bool(store.history(1)), 'История вопроса сохранена')

                    repeat = run_case('registration_cached', QUESTION)
                    check_summary_case(repeat, expected_model_calls=1, allow_clarification=True)
                    check_registration_answer(repeat, require_filling=True)
                    initial_failures = any(stage.get('failures') for stage in first['research_stages'])
                    cache_used = (repeat['source_http_requests'] < first['source_http_requests']
                                  if initial_failures else repeat['source_http_requests'] == 0)
                    check(repeat, cache_used,
                          'Кэш сокращает HTTP; недоступные при первом поиске источники можно повторить')

                    followup = run_case('followup', 'А куда его подавать?')
                    check_summary_case(followup)
                    check_registration_answer(followup, followup=True)

                    fee = run_case('registration_fee', FEE_QUESTION)
                    check_summary_case(fee, expected_model_calls=1)
                    check_fee_answer(fee)
                    state = store.load_profile(1)
                    check(fee, all(state.get(key) == value for key, value in PROFILE.items()),
                          'Поиск и выжимка не изменили сохранённую анкету')

            report['documents'] = store.db.execute('SELECT count(*) AS n FROM documents').fetchone()['n']
        finally:
            if store is not None:
                store.close()
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-model', action='store_true',
                        help='Только разговор и быстрые ответы по анкете, без поиска и нейронной выжимки')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    settings = Settings.from_env(demo=True)
    if not settings.database_url:
        parser.error('Задайте DEMO_DATABASE_URL: проверка создаст и удалит отдельную схему в этой базе.')
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Локальная очередь, Assistant, PostgreSQL, источники и Ollama; приём и доставка MAX не измеряются',
        'reply_limits': {'processing_seconds': PROCESSING_SECONDS, 'summary_characters': 3600,
                         'verbatim_source_characters': 360},
        'source_http_count': 'Попытки HTTP к источникам, включая robots.txt и перенаправления',
        'model': settings.model,
        'model_device_requested': settings.model_device,
        'model_think': settings.model_think,
        'context_tokens': settings.context_tokens,
        'model_threads': settings.model_threads,
        'max_pages': settings.max_pages,
        'source_ttl_hours': settings.source_ttl_hours,
        'skip_model': args.skip_model,
        'cases': [],
        'success': False,
    }
    started = time.perf_counter()
    try:
        benchmark(settings, skip_model=args.skip_model, report=report)
        report['success'] = True
    except Exception as error:
        # DSN, токен и тексты низкоуровневых ошибок в отчёт не попадают.
        report['error_type'] = type(error).__name__
        if isinstance(error, AssertionError):
            report['failed_check'] = str(error)
        print(f'Проверка не пройдена: {report.get("failed_check", report["error_type"])}', flush=True)
    finally:
        report['total_seconds'] = round(time.perf_counter() - started, 6)
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Отчёт: data/performance-check.json', flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
