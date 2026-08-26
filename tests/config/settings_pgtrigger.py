"""
The test project with django-pgtrigger alongside, for the compatibility environment.

pgtrigger is listed last here and first in the sibling module, because the order the apps are readied in decides which
library layers its autodetection onto the other's. Both have to end up with one autodetector carrying both, and this
ordering (pgtrigger patching after this package) is the one that fails if this package writes only the command attribute
and leaves the module-level names behind.
"""

from config.settings import *  # noqa: F403

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'postgres_objects',
    'pgtrigger',
    'pgtrigger_example',
)
