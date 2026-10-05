import re
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile(r'[\u0400-\u052f]')


class LanguageTests(unittest.TestCase):
    def test_tracked_project_text_contains_no_untranslated_cyrillic(self):
        names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z']).decode().split('\0')
        untranslated = []
        for name in filter(None, names):
            path = ROOT / name
            if path.is_file() and CYRILLIC.search(path.read_text()):
                untranslated.append(name)
        self.assertEqual(untranslated, [], 'Project text must be in English')


if __name__ == '__main__':
    unittest.main()
