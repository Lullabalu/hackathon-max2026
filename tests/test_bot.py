import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ip_bot.dialog import QUESTIONS, parse_answer, respond
from ip_bot.max_api import MaxAPI, normalize_update


class DialogTests(unittest.TestCase):
    def test_full_business_survey_and_correction(self):
        state, _ = respond(None, "/begin")
        for answer in ("Разработка сайтов", "Самарская область", "1 200 000,50", "300000", "2", "3"):
            state, reply = respond(state, answer)
        self.assertEqual(len(state["answers"]), len(QUESTIONS))
        self.assertEqual(state["answers"]["revenue"], "1200000.50")
        self.assertIn("Самарская область", reply)
        self.assertIn("Документы никуда не отправлены", reply)
        self.assertLessEqual(len(reply), 4000)
        state, _ = respond(state, "/back")
        state, reply = respond(state, "1")
        self.assertEqual(state["answers"]["clients"], "Физлица")

    def test_invalid_income_does_not_advance_survey(self):
        state, _ = respond(None, "/begin")
        state, _ = respond(state, "Разработка сайтов")
        state, _ = respond(state, "Москва")
        for answer in ("-1", "NaN", "Infinity", "1e6", "1 млн", "2.001"):
            with self.subTest(answer=answer):
                new_state, reply = respond(state, answer)
                self.assertEqual(state, new_state)
                self.assertIn("Вопрос 3", reply)
        self.assertEqual(parse_answer("revenue", "0"), "0.00")

    def test_restart_and_help_do_not_reset_answers(self):
        state, _ = respond(None, "/begin")
        state, _ = respond(state, "Ремонт компьютеров")
        for command in ("/start", "/begin", "/profile", "/help", "/sources", "/unknown"):
            with self.subTest(command=command):
                new_state, _ = respond(state, command)
                self.assertEqual(new_state, state)

    def test_no_business_profile_before_begin(self):
        state, _ = respond(None, "Хочу открыть ИП")
        self.assertIsNone(state)

    def test_expenses_can_exceed_revenue(self):
        state = {"answers": {"activity": "Торговля", "region": "Москва", "revenue": "100.00"}}
        result, _ = respond(state, "200")
        self.assertEqual(result["answers"]["expenses"], "200.00")


class APITests(unittest.TestCase):
    def test_private_message_and_bot_started(self):
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 123, "is_bot": False},
                "recipient": {"chat_type": "dialog", "chat_id": 456, "user_id": 999},
                "body": {"mid": "mid.123", "text": "/begin"},
            },
        }
        self.assertEqual(normalize_update(update), {"event_id": "message:mid.123", "user_id": 123, "text": "/begin"})
        update["message"]["recipient"]["chat_type"] = "chat"
        self.assertIsNone(normalize_update(update))
        update["message"]["recipient"]["chat_type"] = "dialog"
        update["message"]["sender"]["is_bot"] = True
        self.assertIsNone(normalize_update(update))
        started = {"update_type": "bot_started", "timestamp": 123, "user": {"user_id": 12}, "chat_id": 20}
        self.assertEqual(normalize_update(started)["text"], "/start")

    @patch("ip_bot.max_api.time.sleep")
    def test_http_contract_without_real_network(self, sleep):
        api = MaxAPI("example-not-a-real-token")
        requests = []
        class FakeOpener:
            def open(self, request, timeout):
                requests.append(request)
                return io.BytesIO(b'{"updates": [], "marker": 100}')
        api.opener = FakeOpener()
        api.updates(99)
        api.send_message(123, "Привет")
        get_request, post_request = requests
        self.assertEqual(get_request.get_header("Authorization"), "example-not-a-real-token")
        self.assertNotIn("example-not-a-real-token", get_request.full_url)
        self.assertEqual(parse_qs(urlsplit(get_request.full_url).query)["marker"], ["99"])
        self.assertEqual(post_request.get_method(), "POST")
        self.assertEqual(parse_qs(urlsplit(post_request.full_url).query)["user_id"], ["123"])
        self.assertEqual(json.loads(post_request.data)["text"], "Привет")


if __name__ == "__main__":
    unittest.main()
