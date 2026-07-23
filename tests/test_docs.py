import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_FILES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
COMPOSE = ROOT / "docker-compose.yml"


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readmes = {path.name: path.read_text(encoding="utf-8") for path in README_FILES}
        cls.compose = COMPOSE.read_text(encoding="utf-8")

    def test_authentication_examples_forward_env_values_to_the_container(self):
        variables = ("AUTH_USERNAME", "AUTH_PASSWORD", "FLASK_SECRET_KEY")
        for variable in variables:
            self.assertIn(f"${{{variable}:-}}", self.compose)
        for name, readme in self.readmes.items():
            self.assertEqual(
                len(re.findall(r"(?m)^\s*services:\s*$", readme)),
                1,
                f"{name} must contain exactly one complete Compose service example",
            )
            for variable in variables:
                mapping = f"{variable}: ${{{variable}:-}}"
                self.assertEqual(
                    readme.count(mapping),
                    1,
                    f"{name} must contain exactly one Compose mapping for {variable}",
                )

    def test_authentication_guides_recreate_and_verify_without_printing_secrets(self):
        required = (
            "docker compose up -d --force-recreate",
            'test -n "$AUTH_USERNAME" && echo "AUTH_USERNAME: set"',
            'test -n "$AUTH_PASSWORD" && echo "AUTH_PASSWORD: set"',
            "[auth] Login required",
            "[auth] Open access",
        )
        for name, readme in self.readmes.items():
            for text in required:
                self.assertIn(text, readme, f"{name} is missing auth troubleshooting step: {text}")


if __name__ == "__main__":
    unittest.main()
