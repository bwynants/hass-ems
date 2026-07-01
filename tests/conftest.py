"""Make the pure ``ems_core`` package importable during tests.

The component directory (this file's parent's parent) is APPENDED to sys.path,
not prepended: the component contains a ``select.py`` platform module that would
otherwise shadow Python's stdlib ``select`` and break pytest itself. Appending
keeps the stdlib modules winning while still letting ``import ems_core`` resolve.
"""
import os
import sys

_COMPONENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COMPONENT_DIR not in sys.path:
    sys.path.append(_COMPONENT_DIR)
