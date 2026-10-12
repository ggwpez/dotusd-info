"""Check chart rendering and input behavior with lightweight DOM/canvas stand-ins."""
from pathlib import Path
import unittest

from js_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_drawing_hover_keyboard_and_drag_zoom(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        # The server also combines the range module with the app, removing its import.
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += '\ncheckApp(); JSON.stringify(true);'
        self.assertTrue(evaluate(source))
