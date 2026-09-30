"""Сравнение разбора восьми синтетических вопросов реальной локальной Ollama.

python -m scripts.evaluate_planner --model qwen3.5:9b --device auto
Ни PostgreSQL, ни MAX, ни сайты источников не вызываются. Проверяется Plan,
числа и ключевые условия; полная оценка смысла требует ручного просмотра.
"""

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
from unittest.mock import patch

from ip_bot.assistant import Assistant, planner_input
from ip_bot.llm import ModelError, Ollama, Plan
from ip_bot.settings import Settings


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / 'tests/fixtures/planner_cases.json'
DEFAULT_REPORT = ROOT / 'data/planner-quality-check.json'


class CapturingFakeModel:
    def __init__(self):
        self.request = None

    def generate(self, schema, system, content, **kwargs):
        self.request = {'schema': schema, 'system': system, 'content': json.JSONDecoder().raw_decode(content)[0], **kwargs}
        if 'action' in schema.model_fields:
            return schema(action='offtopic', text='', topics=[])
        return Plan(query='', topics=[], keywords=[], clarification='', relevant=False)


class NoResearch:
    def search(self, plan):
        raise AssertionError('Оценка планировщика не должна обращаться к источникам')


def production_request(settings, fixture_date):
    """Берём текущий prompt, схему и список тем у настоящего Assistant."""
    captured = CapturingFakeModel()
    assistant = Assistant(None, settings, model=captured, researcher=NoResearch())
    with patch('ip_bot.assistant.date', SimpleNamespace(today=lambda: fixture_date)):
        assistant.respond(None, 'Куда это подавать и что приложить?')
    if captured.request is None:
        raise ValueError('Не удалось получить актуальный запрос планировщика у Assistant')
    return captured.request


class MeteredOpener:
    """Сохраняет только метрики ответа Ollama, не заголовки или адрес сервера."""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.metadata = {}
        self.raw_content = None

    def open(self, request, **kwargs):
        self.metadata = {}
        self.raw_content = None
        with self.wrapped.open(request, **kwargs) as response:
            raw = response.read(128_001)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return io.BytesIO(raw)
        if isinstance(payload, dict):
            for name in ('done', 'done_reason', 'prompt_eval_count', 'eval_count'):
                if name in payload:
                    self.metadata[name] = payload[name]
            for name in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration'):
                value = payload.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.metadata[name + '_seconds'] = round(value / 1_000_000_000, 6)
            for count, duration, output in (
                ('prompt_eval_count', 'prompt_eval_duration', 'prompt_tokens_per_second'),
                ('eval_count', 'eval_duration', 'output_tokens_per_second'),
            ):
                tokens, nanoseconds = payload.get(count), payload.get(duration)
                if isinstance(tokens, int) and isinstance(nanoseconds, (int, float)) and nanoseconds > 0:
                    self.metadata[output] = round(tokens / (nanoseconds / 1_000_000_000), 3)
            message = payload.get('message')
            if isinstance(message, dict) and isinstance(message.get('content'), str):
                self.raw_content = message['content'][:10_000]
        return io.BytesIO(raw)


def numbers(value):
    return set(re.findall(r'\d+(?:[.,]\d+)?', value))


def evaluate(plan, expected, context, known_topics):
    checks = []

    def add(name, passed, **details):
        checks.append({'name': name, 'passed': bool(passed), **details})

    add('relevant', plan.relevant == expected['relevant'])
    add('clarification_required', bool(plan.clarification.strip()) == expected['clarification_required'])
    actual_topics = set(plan.topics)
    add('topics_known', actual_topics <= known_topics)
    if 'topics_include' in expected:
        add('topics_include', set(expected['topics_include']) <= actual_topics)
    if 'topics_exclude' in expected:
        add('topics_exclude', not (set(expected['topics_exclude']) & actual_topics))
    if 'topics_allowed' in expected:
        add('topics_allowed', actual_topics <= set(expected['topics_allowed']))

    # OR внутри группы допускает перефразирование и русские окончания; AND
    # между группами не позволяет потерять регион, действие или предмет запроса.
    if 'query_term_groups' in expected:
        query = plan.query.casefold().replace('ё', 'е')
        missing = [group for group in expected['query_term_groups']
                   if not any(term.casefold().replace('ё', 'е') in query for term in group)]
        add('query_term_groups', not missing, missing_groups=missing)

    # Проверяем числовые инварианты синтетических примеров, а не дословность
    # перефразирования. Например, возраст 17 нельзя заменить на 18, а 2024 на 2026.
    required_numbers = numbers(' '.join(expected.get('query_preserves', [])))
    query_numbers = numbers(plan.query)
    if required_numbers:
        add('required_query_numbers', required_numbers <= query_numbers,
            expected=sorted(required_numbers), actual=sorted(query_numbers))
    input_numbers = numbers(json.dumps({key: context[key] for key in ('question', 'profile', 'history')}, ensure_ascii=False))
    # Ожидаемый год может быть выведен из «следующего года» и даты фикстуры.
    allowed_numbers = input_numbers | required_numbers
    produced_numbers = numbers(' '.join([plan.query, plan.clarification, *plan.keywords]))
    add('no_unprovided_numbers', produced_numbers <= allowed_numbers,
        unexpected=sorted(produced_numbers - allowed_numbers))
    return checks


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    if fixture.get('synthetic_only') is not True:
        raise SystemExit('Оценка разрешена только на синтетической фикстуре')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, help='Точное имя установленной локальной модели Ollama')
    parser.add_argument('--device', choices=('auto', 'cpu'), default='auto', help='auto допускает Metal/GPU; cpu принудительно использует CPU')
    parser.add_argument('--case', action='append', choices=[case['id'] for case in fixture['cases']], help='Только выбранный кейс; можно повторить аргумент')
    parser.add_argument('--output', type=Path, default=DEFAULT_REPORT, help='JSON-отчёт, по умолчанию data/planner-quality-check.json')
    args = parser.parse_args()
    settings = replace(Settings.from_env(demo=True), model=args.model, model_device=args.device)
    template = production_request(settings, date.fromisoformat(fixture['today']))
    output_schema = template['schema']
    cases = [case for case in fixture['cases'] if not args.case or case['id'] in args.case]
    model = Ollama(settings)
    meter = MeteredOpener(model.opener)
    model.opener = meter
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'fixture': str(FIXTURE.relative_to(ROOT)),
        'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'synthetic_only': True,
        'fixture_today': fixture['today'],
        'model': settings.model,
        'device_requested': settings.model_device,
        'context_tokens': settings.context_tokens,
        'model_threads': settings.model_threads,
        'keep_alive': settings.model_keep_alive,
        'max_tokens': template['max_tokens'],
        'system_prompt': template['system'],
        'schema': output_schema.model_json_schema(),
        'selected_cases': [case['id'] for case in cases],
        'warm_cache_note': 'Прогрев не выполняется. Первый вызов может включать загрузку модели; повторные могут использовать кэш prompt. Смотрите load_duration и wall_seconds.',
        'scope': 'Только реальный Ollama.generate с текущей схемой Assistant, без локальной маршрутизации, источников, базы данных и MAX',
        'manual_review_required': True,
        'cases': [],
    }
    write_report(args.output, report)
    known_topics = {topic['id'] for topic in template['content']['topics']}
    interrupted = False
    for index, case in enumerate(cases):
        item = case['input']
        context = {
            'topics': template['content']['topics'],
            'profile': item.get('profile', {}),
            'history': [
                {'question': turn['question'][:250], 'answer': turn['answer'][:400]}
                for turn in item.get('history', [])[-2:]
            ],
            'question': item['question'],
        }
        result = {
            'id': case['id'], 'call_index': index + 1, 'input': context,
            'expected': case['expected'], 'valid_plan': False,
            'manual_review': {key: value for key, value in case['expected'].items() if key in {
                'query_preserves', 'must_not_invent', 'query_must_not_change_intent_to',
                'superseded_context_not_used_as_active',
            }},
        }
        meter.metadata = {}
        meter.raw_content = None
        started = time.perf_counter()
        try:
            generated = model.generate(output_schema, template['system'], planner_input(context),
                                       max_tokens=template['max_tokens'])
            result['raw_intent'] = generated.model_dump()
            plan = (generated.to_plan(context=context, current_year=date.fromisoformat(fixture['today']).year)
                    if hasattr(generated, 'to_plan') else generated)
            result.update(valid_plan=True, output=plan.model_dump())
            result['checks'] = evaluate(plan, case['expected'], context, known_topics)
            result['automatic_checks_passed'] = all(check['passed'] for check in result['checks'])
        except KeyboardInterrupt:
            interrupted = True
            result.update(error_type='KeyboardInterrupt', automatic_checks_passed=False)
        except Exception as error:
            result.update(error_type=type(error).__name__, automatic_checks_passed=False)
            if isinstance(error, ModelError):
                result['error_code'] = str(error)
            if meter.raw_content is not None:
                result['raw_model_content'] = meter.raw_content
        finally:
            result['wall_seconds'] = round(time.perf_counter() - started, 6)
            result['ollama'] = meter.metadata
            report['cases'].append(result)
            report['automatic_checks_passed'] = all(item['automatic_checks_passed'] for item in report['cases'])
            report['completed'] = len(report['cases']) == len(cases)
            write_report(args.output, report)
            print(f'{case["id"]}: {result["wall_seconds"]:.3f} сек.; '
                  f'JSON={result["valid_plan"]}; проверки={result["automatic_checks_passed"]}', flush=True)
        if interrupted:
            return 130
    print(f'Отчёт: {args.output}. Смысл и отсутствие выдуманных фактов проверьте вручную.', flush=True)
    return 0 if report.get('automatic_checks_passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
