"""
Ordering the object operations, and the cross-app dependencies a view's references impose.
"""

from django.apps import apps
from django.db.migrations.exceptions import CircularDependencyError


def get_split_by_placement(operations):
    """
    Separate operations on objects that exist after the model migrations from those that exist before them.
    """
    after = [operation for operation in operations if not operation.definition.precedes_models]
    before = [operation for operation in operations if operation.definition.precedes_models]

    return after, before


def get_ordered_by_references(operations):
    """
    Order operations so that one reading what another creates comes after that other.
    """
    by_db_name = {}
    for operation in operations:
        by_db_name.setdefault(operation.definition.db_name, operation)

    required = {
        id(operation): {
            id(by_db_name[reference])
            for reference in getattr(operation.definition, 'references', ())
            if reference in by_db_name and by_db_name[reference] is not operation
        }
        for operation in operations
    }

    ordered = []
    placed = set()
    remaining = list(operations)

    while remaining:
        ready = next((operation for operation in remaining if required[id(operation)] <= placed), None)
        if ready is None:
            raise CircularDependencyError(
                'The views {} read from each other, which operations within one migration cannot order. Split the '
                'change over two makemigrations runs.'.format(
                    ', '.join(sorted(operation.definition.db_name for operation in remaining))
                )
            )

        remaining.remove(ready)
        placed.add(id(ready))
        ordered.append(ready)

    return ordered


def get_ordered_leading(operations):
    views, functions = get_split_by_placement(operations)

    return get_ordered_by_references(views)[::-1] + functions


def get_ordered_trailing(operations):
    views, functions = get_split_by_placement(operations)

    return functions + get_ordered_by_references(views)


def get_creating_operations(operations):
    return [operation for operation in operations if getattr(operation, 'creates', False)]


def get_removing_operations(operations):
    return [operation for operation in operations if getattr(operation, 'removes', False)]


def get_references(operations):
    return {reference for operation in operations for reference in getattr(operation.definition, 'references', ())}


def refuse_cyclic_apps(edges, description):
    remaining = {app_label: set(required) for app_label, required in edges.items()}

    while remaining:
        ready = [app_label for app_label, required in remaining.items() if not required & remaining.keys()]
        if not ready:
            raise CircularDependencyError(
                '{} of {} read from each other, which one object migration per app cannot order. Split the change '
                'over two makemigrations runs.'.format(description, ', '.join(sorted(remaining)))
            )

        for app_label in ready:
            del remaining[app_label]


def get_dependency_targets(trailing, trailing_migrations, migrated, graph, covered_apps):
    """
    Calculate the migration that puts each relation a view might read in place, keyed by its SQL identifier.
    """
    targets = {}

    for app_label, migration in trailing_migrations.items():
        for operation in get_creating_operations(trailing[app_label]):
            targets[operation.definition.db_name] = (app_label, migration.name)

    for migrated_object in migrated.values():
        targets.setdefault(migrated_object.definition.db_name, migrated_object.node)

    for model in apps.get_models():
        app_label = model._meta.app_label
        if app_label in covered_apps:
            continue

        leaves = graph.leaf_nodes(app_label)
        if leaves:
            targets.setdefault(model._meta.db_table, leaves[0])

    return targets


def get_create_dependencies(trailing, targets):
    """
    Calculate which migrations each app's trailing object migration has to wait for.
    """
    dependencies = {}

    for app_label, operations in trailing.items():
        dependencies[app_label] = [
            targets[reference]
            for reference in sorted(get_references(get_creating_operations(operations)))
            if reference in targets and targets[reference][0] != app_label
        ]

    return dependencies


def get_drop_dependencies(leading):
    """
    Calculate which apps' leading object migrations have to run first, because they drop a view reading a view this app
    drops.
    """
    dropped = {
        app_label: {operation.definition.db_name for operation in get_removing_operations(operations)}
        for app_label, operations in leading.items()
    }
    reads = {
        app_label: get_references(get_removing_operations(operations)) for app_label, operations in leading.items()
    }

    return {
        app_label: {
            other_app
            for other_app, references in reads.items()
            if other_app != app_label and references & dropped[app_label]
        }
        for app_label in leading
    }
