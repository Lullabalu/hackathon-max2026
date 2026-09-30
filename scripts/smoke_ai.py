"""python -m scripts.smoke_ai: ФНС + PostgreSQL + нейронная выжимка, без MAX."""
import json
import logging
from pathlib import Path
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.__main__ import drain
from ip_bot.assistant import Assistant
from ip_bot.budget import PROCESSING_SECONDS
from ip_bot.llm import Ollama
from ip_bot.settings import Settings
from ip_bot.storage import Store
from ip_bot.sources import Researcher
from scripts.benchmark import answer_metrics


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    settings = Settings.from_env(demo=True)
    if not settings.database_url:
        raise SystemExit('Задайте DEMO_DATABASE_URL: проверка создаст и удалит отдельную схему в этой базе.')
    timings = []
    details = {}
    class TimedModel(Ollama):
        def generate(self, schema, *args, **kwargs):
            started = time.monotonic()
            stage = {'stage': schema.__name__, 'success': False}
            try:
                result = super().generate(schema, *args, **kwargs)
                details[schema.__name__] = result.model_dump()
                stage['success'] = True
                return result
            except BaseException as error:
                stage['error_type'] = type(error).__name__
                raise
            finally:
                stage['seconds'] = round(time.monotonic() - started, 2)
                timings.append(stage)
                print(f'{schema.__name__}: {stage["seconds"]} сек., success={stage["success"]}', flush=True)
    schema = 'smoke_' + uuid.uuid4().hex
    with psycopg.connect(settings.database_url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        store = None
        try:
            store = Store(make_conninfo(settings.database_url, options=f'-c search_path={schema}'), worker=False)
            question = 'Я совершеннолетний гражданин РФ. Какие документы нужны для регистрации ИП?'
            messages = []
            def send(user_id, text):
                messages.append(text)
                print(text, flush=True)
            class CapturedResearcher(Researcher):
                def search(self, plan):
                    passages, failures = super().search(plan)
                    details['passages'] = passages
                    details['failures'] = failures
                    return passages, failures
            assistant = Assistant(store, settings, model=TimedModel(settings), researcher=CapturedResearcher(store, settings))
            store.receive([{'event_id': 'consent', 'user_id': 1, 'text': '/consent'}])
            drain(store, lambda *_: None, assistant)
            store.receive([{'event_id': 'free-chat', 'user_id': 1, 'text': '/chat'}])
            drain(store, lambda *_: None, assistant)
            store.receive([{'event_id': 'smoke', 'user_id': 1, 'text': question}])
            started = time.monotonic()
            drain(store, send, assistant)
            elapsed = round(time.monotonic() - started, 2)
            Path('data').mkdir(exist_ok=True)
            Path('data/ai-integration-details.json').write_text(json.dumps(details, ensure_ascii=False, indent=2, default=str))
            reply = messages[-1] if messages else ''
            visible = ' '.join(reply.casefold().split())
            summaries = [stage for stage in timings if stage['stage'] == 'Summary']
            points = details.get('Summary', {}).get('points', [])
            metrics = answer_metrics(reply, details.get('passages', []))
            checks = {
                'one_reply': len(messages) == 1,
                'summary_succeeded': len(summaries) == 1 and summaries[0]['success'],
                'supported_points': 1 <= len(points) <= 3,
                'summary_reached_user': bool(points) and all(
                    ' '.join(point.get('text', '').casefold().split()) in visible
                    for point in points if point.get('text')),
                'not_fallback': not any(marker in visible for marker in (
                    'не успел', 'не удалось подготовить', 'не удалось составить',
                    'модель сейчас недоступна', 'выдержки из официальных',
                    'недостаточно данных для ответа',
                )),
                'short_answer': 0 < len(reply) <= 1500,
                'not_large_excerpts': metrics['longest_quote_characters'] <= 360
                    and metrics['longest_source_copy_characters'] <= 360,
                'source_citations': '[1]' in reply and 'https://' in reply,
                'registration_form': '21001' in reply,
                'bounded_processing': elapsed <= PROCESSING_SECONDS,
                'bounded_model_calls': 1 <= len(timings) <= 2,
                'queue_drained': store.pending() is None,
                'history_saved': bool(store.history(1)),
            }
            report = {'model': settings.model, 'model_device': settings.model_device,
                      'model_calls': len(timings), 'stages': timings, 'seconds': elapsed,
                      'reply': reply, 'checks': checks, 'success': all(checks.values()), **metrics,
                      'documents': store.db.execute('SELECT count(*) AS n FROM documents').fetchone()['n']}
            Path('data').mkdir(exist_ok=True)
            Path('data/ai-integration-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            assert report['success'], 'Проверка выжимки не пройдена: ' + ', '.join(key for key, passed in checks.items() if not passed)
            print(f'Сквозная проверка пройдена за {elapsed} сек. Отчёт: data/ai-integration-check.json', flush=True)
        finally:
            if store is not None:
                store.close()
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


if __name__ == '__main__':
    main()
