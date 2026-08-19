"""
Autodetection of the objects each app declares, in the module named by POSTGRES_OBJECTS['<object_type>_MODULE_PATH'].

Django's autodetector compares two ProjectStates. This logic reads what ProjectState the migrations have already created
from its generated graph, and adds on logic for our declared objects, so we avoid having to track separate ProjectStates
for our custom objects.
"""

from postgres_objects.autodetector.changes import get_migrated_objects, get_object_changes, get_ordered_nodes
from postgres_objects.autodetector.detector import (
    DeclarativeObjectAutodetector,
    DeclarativeObjectAutodetectorMixin,
    build_migration,
)
from postgres_objects.autodetector.wiring import compose, get_autodetector, patch_migrations

__all__ = [
    'DeclarativeObjectAutodetector',
    'DeclarativeObjectAutodetectorMixin',
    'build_migration',
    'compose',
    'get_autodetector',
    'get_migrated_objects',
    'get_object_changes',
    'get_ordered_nodes',
    'patch_migrations',
]
