"""Keep README.zh-CN.md structurally in step with README.md.

The Chinese README is written alongside the English one rather than left to a
contributor, so it drifts quietly — nothing breaks, it just stops saying the
same things. Two real gaps went unnoticed for weeks because the manual check
was "same number of sections, same number of feature bullets":

  * a feature bullet added to the English side only (v1.15.8), and
  * the "run the tests before opening a PR" block, which changes neither of
    those counts because it is a fenced code block, not a bullet.

Comparing each section's shape catches all of it. These tests are deliberately
strict: one English bullet must be one Chinese bullet. When a failure is a
genuine translation choice rather than drift, change the English to match, or
change both — don't loosen the comparison.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGLISH = ROOT / "README.md"
CHINESE = ROOT / "README.zh-CN.md"

# Things a translated section must mirror one-for-one.
COUNTED = ("bullets", "table rows", "code blocks", "sub-headings", "numbered steps")


def sections(text):
    """Split a README into top-level sections and count their structure.

    Fenced blocks are counted but not looked inside: a '- ' inside a shell
    example is not a bullet, and a '|' in ASCII output is not a table row.
    """
    found, current, in_fence = [], None, False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            if in_fence and current is not None:
                current["code blocks"] += 1
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), **{k: 0 for k in COUNTED}}
            found.append(current)
        elif current is not None:
            if line.startswith("### "):
                current["sub-headings"] += 1
            elif re.match(r"^\s*[-*] ", line):
                current["bullets"] += 1
            elif re.match(r"^\s*\d+\. ", line):
                current["numbered steps"] += 1
            elif line.startswith("|") and not re.match(r"^\|[\s|:-]+\|?$", line):
                current["table rows"] += 1
    return found


def slug(heading):
    """Reproduce GitHub's heading anchors.

    Lowercase, drop punctuation, then replace *each* space with a hyphen.
    Runs are not collapsed, so "Backup & rollback" anchors as
    "backup--rollback" — with two hyphens, because the dropped '&' leaves two
    spaces behind. Collapsing whitespace here makes the link check report
    perfectly good links as broken.
    """
    heading = re.sub(r"[^\w\s一-鿿-]", "", heading.strip().lower())
    return heading.replace(" ", "-")


class TranslationParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = ENGLISH.read_text(encoding="utf-8")
        cls.chinese = CHINESE.read_text(encoding="utf-8")

    def test_same_sections_in_the_same_order(self):
        english, chinese = sections(self.english), sections(self.chinese)
        self.assertEqual(
            len(english), len(chinese),
            f"README.md has {len(english)} top-level sections, "
            f"README.zh-CN.md has {len(chinese)} — a whole section is missing",
        )

    def test_each_section_has_the_same_structure(self):
        english, chinese = sections(self.english), sections(self.chinese)
        drift = []
        for en, zh in zip(english, chinese):
            for key in COUNTED:
                if en[key] != zh[key]:
                    drift.append(
                        f"  {en['heading']!r} / {zh['heading']!r}: "
                        f"{en[key]} {key} in English, {zh[key]} in Chinese"
                    )
        self.assertEqual(
            drift, [],
            "README.zh-CN.md has drifted from README.md:\n" + "\n".join(drift),
        )

    def test_internal_links_point_at_headings_that_exist(self):
        for path, text in ((ENGLISH, self.english), (CHINESE, self.chinese)):
            anchors = {slug(h) for h in re.findall(r"(?m)^#{2,4}\s+(.+)$", text)}
            broken = [a for a in re.findall(r"\]\(#([^)]+)\)", text) if a not in anchors]
            self.assertEqual(broken, [], f"{path.name} links to missing headings: {broken}")

    def test_both_document_the_same_env_vars(self):
        pattern = r"`([A-Z][A-Z0-9_]{3,})`"
        english = set(re.findall(pattern, self.english))
        chinese = set(re.findall(pattern, self.chinese))
        self.assertEqual(
            sorted(english - chinese), [],
            "in README.md but missing from README.zh-CN.md",
        )
        self.assertEqual(
            sorted(chinese - english), [],
            "in README.zh-CN.md but missing from README.md",
        )


if __name__ == "__main__":
    unittest.main()
