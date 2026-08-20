"""
System checks for view declarations.

A queryset-declared view can be wrong in ways raw SQL cannot: its body compiles, its model derives a primary key, and
both happen lazily. Left to laziness alone, a mistake would surface at the first .objects access, which may be in
production. Building everything here instead moves the failure to `manage.py check`, and therefore to runserver, migrate
and CI.
"""

from django.core.checks import Error, register

from postgres_objects.registry import get_declared_objects, get_views_module
from postgres_objects.views import View


@register('postgres_objects')
def check_view_declarations(app_configs, **kwargs):
    """
    Build every queryset-declared view's definition and model, reporting what (if anything) raises.
    """
    module_path = get_views_module()
    if not module_path:
        return []

    errors = []

    for declaration in get_declared_objects(module_path, kind=View).values():
        if declaration.sql and not declaration.queryset:
            # A raw-sql declaration has nothing to compile and no model to build; whether its SQL holds up is for
            # Postgres to say at migrate time.
            continue

        try:
            declaration.definition
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            errors.append(
                Error(
                    'The view declaration could not be compiled: {}'.format(error),
                    obj=declaration,
                    id='postgres_objects.E001',
                )
            )
            continue

        try:
            declaration.model
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            errors.append(
                Error(
                    'The model for the view declaration could not be built: {}'.format(error),
                    obj=declaration,
                    id='postgres_objects.E002',
                )
            )

    return errors
