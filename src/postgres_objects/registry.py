"""
Collecting the objects each app declares.
"""

from importlib import import_module

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from postgres_objects.base import DeclarativeObject, DeclarativeObjectMeta


def get_options():
    """
    The package's settings dict, or an empty one when the project never configured it.
    """
    return getattr(settings, 'POSTGRES_OBJECTS', {})


def get_functions_module():
    """
    The module path function declarations are read from, relative to each app, or None when functions are not managed.
    """
    return get_options().get('FUNCTIONS_MODULE_PATH', None)


def get_views_module():
    """
    The module path view declarations are read from, relative to each app, or None when views are not managed.
    """
    return get_options().get('VIEWS_MODULE_PATH', None)


def import_app_module(app_config, module_path):
    """
    Import a module from within an app, or return None when the app does not have one.
    """
    dotted_path = '{}.{}'.format(app_config.name, module_path)

    try:
        return import_module(dotted_path)
    except ModuleNotFoundError as error:
        # An app not having the module is fine (it just doesn't have this particular type of object), but a broken
        # import *inside* the module is an error.
        if error.name is None or (error.name != dotted_path and not dotted_path.startswith(error.name + '.')):
            raise

        return None


def get_declared_objects(module_path, kind=DeclarativeObject):
    """
    Collect the objects declared at module level in the given module of every installed app.

    module_path is relative to each app, and may be dotted, so both 'db_functions' and 'db.functions' work.

    Returns a dict keyed by (app_label, name), which is the same key the migration graph is folded into when working out
    what has changed. A declaration imported into another app's module is attributed to the app it was written in, not
    the one importing it, so collecting it twice is harmless.
    """
    declared = {}
    by_db_name = {}

    for app_config in apps.get_app_configs():
        module = import_app_module(app_config, module_path)
        if module is None:
            continue

        for declaration in vars(module).values():
            if not isinstance(declaration, DeclarativeObjectMeta) or not issubclass(declaration, kind):
                continue

            if declaration.abstract:
                continue

            app_label = declaration.resolved_app_label

            existing = declared.get((app_label, declaration.name))
            if existing is not None and existing is not declaration:
                raise ImproperlyConfigured(
                    "The '{}' app declares two different objects named '{}'.".format(app_label, declaration.name)
                )

            clashing = by_db_name.get(declaration.resolved_db_name)
            if clashing is not None and clashing is not declaration:
                raise ImproperlyConfigured(
                    "'{}' and '{}' would both be created as '{}'. Set db_name on one of them.".format(
                        clashing.__name__, declaration.__name__, declaration.resolved_db_name
                    )
                )

            by_db_name[declaration.resolved_db_name] = declaration
            declared[(app_label, declaration.name)] = declaration

    return declared
