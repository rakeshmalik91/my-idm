"""Pytest configuration and global fixtures for My-IDM test suite.

CRITICAL: Protect user settings in Windows Registry from being overwritten or cleared
during test execution or development. All QSettings instances in tests must be isolated
to a temporary directory using IniFormat.
"""

import shutil
import tempfile
import pytest
from PySide6.QtCore import QSettings

# Global session fixture to isolate QSettings away from the host OS registry / config
_orig_qsettings_init = QSettings.__init__
_test_settings_dir = tempfile.mkdtemp(prefix="my_idm_test_settings_")


def _isolated_qsettings_init(self, *args, **kwargs):
    # If called with default app ("MyIDM", "My-IDM") or no args, force isolated IniFormat in temp dir
    if len(args) == 0:
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MyIDM", "My-IDM")
        return
    if len(args) == 2 and args[0] == "MyIDM" and args[1] == "My-IDM":
        _orig_qsettings_init(self, QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MyIDM", "My-IDM")
        return
    _orig_qsettings_init(self, *args, **kwargs)


# Patch QSettings globally for tests
QSettings.__init__ = _isolated_qsettings_init
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _test_settings_dir)


@pytest.fixture(autouse=True, scope="session")
def isolate_qsettings_session():
    """Ensure user settings in registry / OS are never touched during test execution."""
    yield
    try:
        shutil.rmtree(_test_settings_dir, ignore_errors=True)
    except Exception:
        pass
