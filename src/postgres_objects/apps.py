from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _validate_module_path(options, key, example):
    """
    Refuse a declarations module path that is not a module path. Unset is interpreted as "do not use this feature".
    """
    module_path = options.get(key, None)
    if module_path is not None and not isinstance(module_path, str):
        raise ImproperlyConfigured(
            "POSTGRES_OBJECTS['{}'] must be the module path to read declarations from, relative to each app, "
            "e.g. '{}'.".format(key, example)
        )


class PostgresObjectsConfig(AppConfig):
    name = 'postgres_objects'
    verbose_name = 'PostgreSQL objects'

    def ready(self):
        options = getattr(settings, 'POSTGRES_OBJECTS', {})
        if not isinstance(options, dict):
            raise ImproperlyConfigured('The POSTGRES_OBJECTS setting must be a dict.')

        _validate_module_path(options, 'FUNCTIONS_MODULE_PATH', 'db_functions')

        # Wire into the Django migration autodetector with a patch so that we can exist next to autodetector wiring for
        # potential other libraries.
        from postgres_objects.autodetector import patch_migrations  # noqa: PLC0415

        patch_migrations()
