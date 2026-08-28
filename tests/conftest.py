"""Make src/ importable for every test module.

Several tests do their own ``sys.path.insert(... "src")`` and the rest
relied on that side effect, which only worked when the whole directory
was collected (an alphabetically-earlier module ran first). Running a
single file -- ``pytest tests/test_rtl_ssr.py`` -- failed to import.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
