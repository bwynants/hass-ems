"""Make the pure ``ems_core`` package importable during tests.

The integration directory (``custom_components/energy_management_system``) is
APPENDED to sys.path, not prepended: it contains a ``select.py`` platform module
that would otherwise shadow Python's stdlib ``select`` and break pytest itself.
Appending keeps the stdlib modules winning while still letting ``import
ems_core`` resolve — without triggering the integration ``__init__.py`` (which
requires ``homeassistant`` to be installed).
"""
import pathlib
import sys

_INTEGRATION_DIR = str(
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "energy_management_system"
)
if _INTEGRATION_DIR not in sys.path:
    sys.path.append(_INTEGRATION_DIR)
