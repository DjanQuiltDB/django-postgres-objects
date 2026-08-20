"""
System checks for declarations.

Two things are checked here, both of which would otherwise fail late or not at all:
 * A queryset-declared view can be wrong in ways raw SQL cannot: its body compiles, its model derives a primary key, and
   both happen lazily. Left to laziness alone, a mistake would surface at the first .objects access, which may be in
   production. Building everything here instead moves the failure to `manage.py check`, and therefore to runserver,
   migrate and CI.
 * A generated column declared with this package's field can be wrong without anything raising: if it resolves no
   postgres_objects.Function declaration, nothing will ever recalculate it, so it is a plain GeneratedField wearing a
   misleading name. Nothing would ever complain about that, which is exactly why it is checked.
"""

from django.apps import apps
from django.core.checks import Error, register

from postgres_objects.fields import GeneratedField
from postgres_objects.functions import Function
from postgres_objects.registry import get_declared_objects, get_functions_module, get_views_module
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


def get_recalculating_fields(app_configs):
    """
    Every generated column declared with this package's field that still expects to be recalculated.

    Read from local_fields rather than get_fields() so an inherited column is checked once, on the model that declares
    it, instead of again on each child. A field opted out with recalculate=False is not collected at all.
    """
    models = (
        apps.get_models() if app_configs is None else [model for config in app_configs for model in config.get_models()]
    )

    return [
        field
        for model in models
        for field in model._meta.local_fields
        if isinstance(field, GeneratedField) and field.recalculate
    ]


def virtual_column_referencing_function_error(field):
    return Error(
        'A virtual generated column cannot reference a postgres_objects.Function declaration, so its '
        'values are never stored and never recalculated.',
        hint='Pass db_persist=True to store the column, or recalculate=False to use it as a plain field.',
        obj=field,
        id='postgres_objects.E005',
    )


@register('postgres_objects')
def check_generated_field_dependencies(app_configs, **kwargs):
    """
    Refuse a generated column that can never be recalculated.
    """
    fields = get_recalculating_fields(app_configs)
    if not fields:
        return []

    module_path = get_functions_module()
    if not module_path:
        return [
            virtual_column_referencing_function_error(field)
            if not field.db_persist
            else Error(
                'The column is declared with postgres_objects.GeneratedField, but no postgres_objects.Function '
                'declarations are managed, so its values can never be recalculated.',
                hint=(
                    "Set POSTGRES_OBJECTS['FUNCTIONS_MODULE_PATH'], or pass recalculate=False to accept that nothing "
                    'will be recomputed.'
                ),
                obj=field,
                id='postgres_objects.E003',
            )
            for field in fields
        ]

    declared = {
        declaration.resolved_db_name.lower()
        for declaration in get_declared_objects(module_path, kind=Function).values()
    }

    errors = []

    for field in fields:
        unknown = sorted(name for name in field.recalculate_on if name.lower() not in declared)
        if unknown:
            errors.append(
                Error(
                    'recalculate_on names {}, which no postgres_objects.Function declaration is called.'.format(
                        ', '.join(unknown)
                    ),
                    hint='Check the spelling, or whether the declaration still exists.',
                    obj=field,
                    id='postgres_objects.E004',
                )
            )

        if not field.db_persist:
            # Postgres refuses a user-defined function in a virtual column's expression, so there is no arrangement of
            # arguments that would make this one recalculate. Reported instead of the more general E006 below, whose
            # advice would be to reference a function it cannot reference.
            errors.append(virtual_column_referencing_function_error(field))
            continue

        # Not reported on top of E004: that already names the same problem, with something actionable to fix.
        if not unknown and not field.referenced_function_names() & declared:
            errors.append(
                Error(
                    'The column references no postgres_objects.Function declaration, so its values are never '
                    'recalculated.',
                    hint=(
                        'Call a postgres_objects.Function declaration in the expression, name one in recalculate_on, '
                        'or pass recalculate=False to use django.db.models.GeneratedField behaviour deliberately.'
                    ),
                    obj=field,
                    id='postgres_objects.E006',
                )
            )

    return errors
