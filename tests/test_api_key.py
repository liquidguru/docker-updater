import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from tests.app_loader import load_app_module

KEY = "k" * 20


class ApiKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_app_module()

    def setUp(self):
        self.mod.app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
        self.mod.load_state = lambda: {"available": {}, "deferred": {}}
        self.mod.save_state = lambda _state: None
        self.mod.GITHUB_WEBHOOK_SECRET = ""
        self.mod.AUTH_ENABLED = True
        self.mod.AUTH_USERNAME = "admin"
        self.mod.AUTH_PASSWORD = "correct horse"
        self.mod.API_KEY = KEY
        self.client = self.mod.app.test_client()

    def bearer(self, key=KEY, scheme="Bearer"):
        return {"Authorization": f"{scheme} {key}"}

    def test_valid_key_reads_status_without_a_session(self):
        r = self.client.get("/api/status", headers=self.bearer())
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 302)

    def test_scheme_is_case_insensitive(self):
        r = self.client.get("/api/status", headers=self.bearer(scheme="bearer"))
        self.assertNotEqual(r.status_code, 401)

    def test_wrong_key_is_rejected(self):
        for key in ("x" * 20, KEY[:-1], KEY + "x", ""):
            r = self.client.get("/api/status", headers=self.bearer(key))
            self.assertEqual(r.status_code, 401, key)

    def test_wrong_scheme_is_rejected(self):
        r = self.client.get("/api/status", headers=self.bearer(scheme="Basic"))
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/api/status", headers={"Authorization": KEY})
        self.assertEqual(r.status_code, 401)

    def test_no_header_is_rejected(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_key_does_not_unlock_any_other_endpoint(self):
        for path in ("/api/containers", "/api/settings", "/api/backups",
                     "/api/dangling-images", "/api/status/extra", "/", "/api/status/"):
            r = self.client.get(path, headers=self.bearer())
            self.assertIn(r.status_code, (401, 302, 404), path)
            self.assertNotEqual(r.status_code, 200, path)

    def test_key_does_not_allow_writes(self):
        for method, path in (("post", "/api/status"), ("put", "/api/status"),
                             ("delete", "/api/status"),
                             ("post", "/api/check"), ("post", "/api/update/x")):
            r = getattr(self.client, method)(path, headers=self.bearer())
            self.assertIn(r.status_code, (401, 302, 404, 405), (method, path))
            self.assertNotEqual(r.status_code, 200, (method, path))

    def test_only_get_is_allowed(self):
        r = self.client.head("/api/status", headers=self.bearer())
        self.assertEqual(r.status_code, 401)
        r = self.client.options("/api/status", headers=self.bearer())
        self.assertEqual(r.status_code, 401)

    def test_key_does_not_create_a_session(self):
        self.client.get("/api/status", headers=self.bearer())
        r = self.client.get("/api/containers")
        self.assertEqual(r.status_code, 401)

    def test_login_password_is_not_a_valid_key(self):
        r = self.client.get("/api/status", headers=self.bearer("correct horse"))
        self.assertEqual(r.status_code, 401)

    def test_unset_key_never_matches(self):
        self.mod.API_KEY = ""
        for key in ("", " ", "None"):
            r = self.client.get("/api/status", headers=self.bearer(key))
            self.assertEqual(r.status_code, 401, repr(key))

    def test_normal_login_still_works(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        self.assertNotEqual(self.client.get("/api/status").status_code, 401)


class ApiKeyConfigTests(unittest.TestCase):
    def load(self, **env):
        base = {"AUTH_USERNAME": "", "AUTH_PASSWORD": "", "API_KEY": ""}
        base.update(env)
        out = StringIO()
        with patch.dict(os.environ, base), redirect_stdout(out):
            mod = load_app_module()
        return mod, out.getvalue()

    def test_key_active_when_login_enabled(self):
        mod, _ = self.load(AUTH_USERNAME="a", AUTH_PASSWORD="b", API_KEY=KEY)
        self.assertEqual(mod.API_KEY, KEY)

    def test_key_is_trimmed(self):
        mod, _ = self.load(AUTH_USERNAME="a", AUTH_PASSWORD="b", API_KEY=f"  {KEY}\n")
        self.assertEqual(mod.API_KEY, KEY)

    def test_short_key_is_ignored_with_a_warning(self):
        mod, out = self.load(AUTH_USERNAME="a", AUTH_PASSWORD="b", API_KEY="short")
        self.assertEqual(mod.API_KEY, "")
        self.assertIn("shorter than", out)
        self.assertNotIn("short\n", out.replace("shorter", ""))

    def test_key_without_login_is_inert(self):
        mod, out = self.load(API_KEY=KEY)
        self.assertEqual(mod.API_KEY, "")
        self.assertIn("no effect", out)

    def test_key_value_is_never_printed(self):
        _, out = self.load(AUTH_USERNAME="a", AUTH_PASSWORD="b", API_KEY=KEY)
        self.assertNotIn(KEY, out)

    def test_unset_by_default(self):
        mod, _ = self.load(AUTH_USERNAME="a", AUTH_PASSWORD="b")
        self.assertEqual(mod.API_KEY, "")


if __name__ == "__main__":
    unittest.main()
