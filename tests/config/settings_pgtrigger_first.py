"""
The compatibility project with pgtrigger readied before this package, the mirror of ``config.settings_pgtrigger``.
"""

from config.settings_pgtrigger import *  # noqa: F403

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'pgtrigger',
    'postgres_objects',
    'pgtrigger_example',
)
