import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.app_loader import load_app_module


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod.app.permanent_session_lifetime = timedelta(days=7)
        self.mod.load_state = lambda: {}
        self.mod.save_state = lambda _state: None
        self.mod.GITHUB_WEBHOOK_SECRET = ""
        self.mod.AUTH_ENABLED = False
        self.mod.AUTH_USERNAME = ""
        self.mod.AUTH_PASSWORD = ""

    def enable_auth(self):
        self.mod.AUTH_ENABLED = True
        self.mod.AUTH_USERNAME = "admin"
        self.mod.AUTH_PASSWORD = "correct horse"

    def authenticated_client(self):
        client = self.mod.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess.permanent = True
        return client

    def test_open_access_when_auth_is_disabled(self):
        response = self.mod.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Docker Updater", response.data)

    def test_protected_page_redirects_to_login(self):
        self.enable_auth()
        response = self.mod.app.test_client().get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/")

    def test_protected_api_returns_json_401(self):
        self.enable_auth()
        response = self.mod.app.test_client().get("/api/settings")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Unauthorized"})

    def test_static_and_webhook_routes_remain_public(self):
        self.enable_auth()
        client = self.mod.app.test_client()
        static_response = client.get("/static/i18n.js")
        self.assertEqual(static_response.status_code, 200)
        static_response.close()
        response = client.post("/webhook/github", json={})
        self.assertEqual(response.status_code, 200)

    def test_correct_login_sets_seven_day_session(self):
        self.enable_auth()
        response = self.mod.app.test_client().post(
            "/login?next=/", data={"username": "admin", "password": "correct horse"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Expires=", cookie)

    def test_wrong_login_is_rejected(self):
        self.enable_auth()
        response = self.mod.app.test_client().post(
            "/login", data={"username": "admin", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)

    def test_logout_is_post_only(self):
        self.enable_auth()
        response = self.authenticated_client().get("/logout")
        self.assertEqual(response.status_code, 405)

    def test_protocol_relative_next_url_is_rejected(self):
        self.enable_auth()
        response = self.mod.app.test_client().post(
            "/login?next=//evil.example/path",
            data={"username": "admin", "password": "correct horse"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_backslash_next_url_is_rejected(self):
        self.enable_auth()
        response = self.mod.app.test_client().post(
            "/login",
            query_string={"next": "/\\evil.example/path"},
            data={"username": "admin", "password": "correct horse"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_successful_login_clears_existing_session_data(self):
        self.enable_auth()
        client = self.mod.app.test_client()
        with client.session_transaction() as sess:
            sess["stale"] = "value"
        response = client.post(
            "/login", data={"username": "admin", "password": "correct horse"}
        )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as sess:
            self.assertNotIn("stale", sess)
            self.assertTrue(sess["authenticated"])


class SecretKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def test_generated_secret_key_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"FLASK_SECRET_KEY": ""}, clear=False), \
                    patch.object(self.mod, "DATA_DIR", tmp):
                first = self.mod._load_or_create_secret_key()
                second = self.mod._load_or_create_secret_key()
            self.assertEqual(first, second)
            self.assertEqual((Path(tmp) / ".secret_key").read_text(encoding="utf-8"), first)


class LanguageSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.state = {}
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod.AUTH_ENABLED = False
        self.mod.load_state = lambda: dict(self.state)
        self.mod.save_state = lambda state: self._save(state)
        self.mod._next_check_time = lambda: None

    def _save(self, state):
        self.state.clear()
        self.state.update(state)

    def test_manual_language_persists_and_overrides_client_hint(self):
        client = self.mod.app.test_client()
        response = client.post(
            "/api/settings", json={"ui_language": "zh-CN", "client_lang": "en"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state["ui_language"], "zh-CN")
        self.assertEqual(self.mod.get_ui_lang(), "zh-CN")
        self.assertIn("可用更新", self.mod.ui_t("notify.updates_title", count=1))
        self.assertEqual(self.mod.ui_t("notify.local_host"), "本地")

    def test_auto_language_clears_manual_override(self):
        self.state.update({"ui_language": "zh-CN", "client_lang": "zh-CN"})
        response = self.mod.app.test_client().post(
            "/api/settings", json={"ui_language": None, "client_lang": "en"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ui_language", self.state)
        self.assertEqual(self.state["client_lang"], "en")
        self.assertEqual(self.mod.get_ui_lang(), "en")

    def test_github_webhook_notification_uses_ui_language(self):
        self.state.update({"ui_language": "zh-CN"})
        sent = []
        self.mod.send_notification = lambda title, body: sent.append((title, body))
        with redirect_stdout(io.StringIO()):
            response = self.mod.app.test_client().post(
                "/webhook/github",
                headers={"X-GitHub-Event": "issues"},
                json={
                    "action": "opened",
                    "repository": {"full_name": "owner/repo"},
                    "issue": {"number": 12, "title": "Bug", "html_url": "https://example.test/12"},
                    "sender": {"login": "alice"},
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(sent), 1)
        self.assertIn("新 Issue", sent[0][0])
        self.assertIn("由 alice 创建", sent[0][1])


if __name__ == "__main__":
    unittest.main()
