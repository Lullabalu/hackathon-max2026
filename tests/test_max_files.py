import base64
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ip_bot.documents import make_pdf
from ip_bot.max_api import APIError, MaxAPI, keyboard_for


class MaxFileTests(unittest.TestCase):
    def setUp(self):
        self.api = MaxAPI('example-test-only-token')
        self.file = {'name': 'ip-checklist.pdf', 'content': base64.b64encode(make_pdf('Документы', 'Тест')).decode()}

    def test_upload_contract_pins_public_destination_and_never_forwards_bot_token(self):
        self.api.request = Mock(return_value={'url': 'https://fu.oneme.ru/api/upload.do?sig=test'})
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"fileId":123,"token":"uploaded-file-token"}'
        with patch('ip_bot.sources.URLPolicy.addresses', return_value=['8.8.8.8']), patch('ip_bot.sources.PinnedHTTPS', return_value=connection) as pinned:
            result = self.api.upload_file(self.file)
        self.api.request.assert_called_once_with('POST', '/uploads', params={'type': 'file'})
        self.assertEqual(pinned.call_args.args[:2], ('fu.oneme.ru', ['8.8.8.8']))
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/api/upload.do?sig=test'))
        self.assertIn('multipart/form-data', kwargs['headers']['Content-Type'])
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertNotIn(self.api.token.encode(), kwargs['body'])
        self.assertIn(b'name="data"; filename="ip-checklist.pdf"', kwargs['body'])
        self.assertEqual(result, {'type': 'file', 'payload': {'token': 'uploaded-file-token'}})
        connection.close.assert_called_once()

    def test_untrusted_or_private_upload_destinations_are_rejected(self):
        for url in ('http://fu.oneme.ru/a', 'https://fu.oneme.ru.evil.example/a', 'https://127.0.0.1/a', 'https://user:password@fu.oneme.ru/a'):
            self.api.request = Mock(return_value={'url': url})
            with patch('ip_bot.sources.PinnedHTTPS') as connect, self.assertRaises(APIError):
                self.api.upload_file(self.file)
            connect.assert_not_called()
        self.api.request = Mock(return_value={'url': 'https://fu.oneme.ru/a'})
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), self.assertRaises(APIError):
            self.api.upload_file(self.file)

    def test_processing_retry_keeps_uploaded_token(self):
        attachment = {'type': 'file', 'payload': {'token': 'test-upload'}}
        self.api.upload_file = Mock(return_value=attachment)
        self.api.send_message = Mock(side_effect=[APIError('not ready', retryable=True), {'success': True}])
        with self.assertRaises(APIError):
            self.api.send_files(1, 'PDF', [self.file])
        self.assertTrue(self.api.send_files(1, 'PDF', [self.file])['success'])
        self.api.upload_file.assert_called_once()
        self.assertEqual(self.api.send_message.call_args.kwargs['attachments'], [attachment])
        self.assertEqual(self.api.uploaded_files, {})

    def test_attachment_processing_error_is_retryable_without_leaking_body(self):
        error = HTTPError('https://platform-api2.max.ru/messages', 400, 'Bad Request', {},
                          io.BytesIO(b'{"code":"attachment.not.ready","message":"private response"}'))
        self.api.opener.open = Mock(side_effect=error)
        with self.assertRaises(APIError) as caught:
            self.api.request('POST', '/messages')
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('private', str(caught.exception))

    def test_message_keyboard_has_only_explicit_commands(self):
        keyboard = keyboard_for('/consent — согласен\n/decline — отказаться')
        self.assertEqual(keyboard[0]['payload']['buttons'][0], [
            {'type': 'message', 'text': '/consent'}, {'type': 'message', 'text': '/decline'}])
        self.assertEqual(keyboard_for('Обычный ответ о документах'), [])

    def test_guided_buttons_use_displayed_labels_and_round_trip_through_assistant(self):
        from ip_bot.assistant import Assistant
        from ip_bot.settings import Settings
        assistant = Assistant(None, Settings(), model=Mock(), researcher=Mock())
        reply = assistant.respond(None, '/consent')
        keyboard = keyboard_for(reply.text)
        labels = [button['text'] for row in keyboard[0]['payload']['buttons'] for button in row]
        self.assertEqual(labels, ['1 — Расскажите об ИП', '2 — Подобрать налоговый режим', '3 — Подготовиться к регистрации'])
        result = assistant.respond(reply.state, labels[2])
        self.assertEqual(result.state['guide']['stage'], 'activity')
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()
        self.api.opener.open = Mock(return_value=io.BytesIO(b'{"success":true}'))
        self.api.send_message(1, '/menu — меню')
        body = json.loads(self.api.opener.open.call_args.args[0].data)
        self.assertEqual(body['attachments'][0]['type'], 'inline_keyboard')


if __name__ == '__main__':
    unittest.main()
