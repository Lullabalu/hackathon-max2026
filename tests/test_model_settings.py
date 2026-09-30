import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ip_bot.llm import ModelError, Ollama, Plan
from ip_bot.settings import Settings


class ModelSettingsTests(unittest.TestCase):
    def request_body(self, settings):
        captured = []
        result = Plan(query='Регистрация ИП', topics=['registration'],
                      keywords=['регистрация'], clarification='', relevant=True)

        class Opener:
            def open(self, request, timeout):
                captured.append(json.loads(request.data))
                return io.BytesIO(json.dumps({'done': True, 'message': {
                    'content': result.model_dump_json()}}).encode())

        model = Ollama(settings)
        model.opener = Opener()
        self.assertEqual(model.generate(Plan, 'system', 'question'), result)
        return captured[0]

    def test_cpu_default_is_explicit_and_keeps_model_loaded(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.model_device, 'cpu')
        self.assertEqual(settings.context_tokens, 4096)
        self.assertEqual(settings.model_keep_alive, '30m')
        body = self.request_body(settings)
        self.assertEqual(body['options']['num_gpu'], 0)
        self.assertEqual(body['options']['num_ctx'], 4096)
        self.assertEqual(body['keep_alive'], '30m')

    def test_auto_leaves_hardware_selection_to_native_ollama(self):
        with patch.dict(os.environ, {'AI_DEVICE': 'auto', 'AI_KEEP_ALIVE': '1h'}, clear=True):
            settings = Settings.from_env()
        body = self.request_body(settings)
        self.assertNotIn('num_gpu', body['options'])
        self.assertEqual(body['keep_alive'], '1h')
        self.assertFalse(body['think'])
        self.assertFalse(body['stream'])
        self.assertEqual(body['format'], Plan.model_json_schema())

    def test_unknown_device_fails_before_any_model_request(self):
        for value in ['gpu', '', 'metal', 'nvidia', '0']:
            with self.subTest(value=value):
                with patch.dict(os.environ, {'AI_DEVICE': value}, clear=True):
                    with self.assertRaisesRegex(ValueError, 'AI_DEVICE'):
                        Settings.from_env()
                with self.assertRaisesRegex(ValueError, 'AI_DEVICE'):
                    Settings(model_device=value)

    def test_keep_alive_duration_validation(self):
        for value in ['1s', '15m', '2h', '999m']:
            with self.subTest(value=value):
                self.assertEqual(Settings(model_keep_alive=value).model_keep_alive, value)
        for value in ['', '-1', '0', '0m', 'forever', '1000h', '30minutes', '1m;bad']:
            with self.subTest(value=value):
                with patch.dict(os.environ, {'AI_KEEP_ALIVE': value}, clear=True):
                    with self.assertRaisesRegex(ValueError, 'AI_KEEP_ALIVE'):
                        Settings.from_env()

    def test_context_and_threads_remain_configurable(self):
        with patch.dict(os.environ, {'AI_CONTEXT_TOKENS': '8192', 'AI_THREADS': '6'}, clear=True):
            settings = Settings.from_env()
        body = self.request_body(settings)
        self.assertEqual(body['options']['num_ctx'], 8192)
        self.assertEqual(body['options']['num_thread'], 6)
        with patch.dict(os.environ, {'AI_CONTEXT_TOKENS': '2048'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'AI_CONTEXT_TOKENS'):
                Settings.from_env()

    def test_warmup_loads_without_prompt_using_generation_hardware_options(self):
        for device, timeout, expected_timeout in [('cpu', 600, 20), ('auto', 12, 12)]:
            with self.subTest(device=device):
                settings = Settings(model_device=device, model_timeout=timeout, model_keep_alive='1h',
                                    context_tokens=8192, model_threads=6)
                generated = self.request_body(settings)
                expected_options = dict(generated['options'])
                expected_options.pop('num_predict')
                model = Ollama(settings)
                with patch.object(model.opener, 'open', return_value=io.BytesIO(b'{"done":true,"done_reason":"load"}')) as opened:
                    self.assertIsNone(model.warmup())
                request = opened.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, settings.ollama_url + '/api/chat')
                self.assertEqual(request.get_method(), 'POST')
                self.assertLessEqual(opened.call_args.kwargs['timeout'], expected_timeout)
                self.assertGreater(opened.call_args.kwargs['timeout'], expected_timeout - 1)
                self.assertEqual(body['messages'], [])
                self.assertEqual(body['options'], expected_options)
                self.assertEqual(body['keep_alive'], '1h')
                self.assertEqual(body['model'], settings.model)
                self.assertFalse(body['stream'])
                self.assertFalse(body['think'])
                self.assertNotIn('format', body)

    def test_warmup_rejects_incomplete_invalid_and_oversized_responses(self):
        for raw, code in [
            (b'{"done":false}', 'model_warmup_incomplete_response'),
            (b'{"done":"true"}', 'model_warmup_incomplete_response'),
            (b'{"done":true,"error":"private server diagnostic"}', 'model_warmup_incomplete_response'),
            (b'[]', 'model_warmup_invalid_response'),
            (b'not json', 'model_warmup_invalid_response'),
            (b' ' * 16_001, 'model_warmup_response_too_large'),
        ]:
            with self.subTest(code=code, sample=raw[:40]):
                model = Ollama(Settings())
                with patch.object(model.opener, 'open', return_value=io.BytesIO(raw)):
                    with self.assertRaises(ModelError) as caught:
                        model.warmup()
                self.assertEqual(str(caught.exception), code)

    def test_warmup_network_failure_is_safe_and_does_not_retry(self):
        model = Ollama(Settings())
        for error in [TimeoutError('private server diagnostic'), URLError('private server diagnostic')]:
            with self.subTest(error=type(error).__name__):
                with patch.object(model.opener, 'open', side_effect=error) as opened:
                    expected = 'model_warmup_timeout' if isinstance(error, TimeoutError) else 'model_warmup_unavailable'
                    with self.assertRaisesRegex(ModelError, '^' + expected + '$'):
                        model.warmup()
                opened.assert_called_once()


if __name__ == '__main__':
    unittest.main()
