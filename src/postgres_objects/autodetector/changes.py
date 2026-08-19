"""
What the migrations have created so far, what is declared right now, and the operations that close the gap.
"""

from collections import defaultdict

from postgres_objects.autodetector.ordering import get_ordered_leading, get_ordered_trailing
from postgres_objects.base import Change
from postgres_objects.functions import Function
from postgres_objects.operations import DatabaseObjectOperation
from postgres_objects.registry import get_declared_objects, get_functions_module, get_views_module
from postgres_objects.views import View

#: Each kind of managed object, as (kind, declaration base, the module path naming where it is declared). The kind
#: strings match ObjectDefinition.kind, which is what the fold over the migration graph keys on.
KINDS = (
    ('function', Function, get_functions_module),
    ('view', View, get_views_module),
)


def get_ordered_nodes(graph):
    """
    Return every node of the migration graph in an order where a migration always follows the ones it depends on.
    """
    return graph._generate_plan(graph.leaf_nodes(), at_end=True)


def get_migrated_objects(graph):
    """
    Fold the object operations over the migration graph to get what the migrations have created so far.

    Returns a dict keyed by (app_label, kind, name), whose values are the (definition, hints) pair the last operation
    touching that object recorded. An object belongs to the app whose migration created it.
    """
    migrated = {}

    for node in get_ordered_nodes(graph):
        app_label, _ = node

        for operation in graph.nodes[node].operations:
            if not isinstance(operation, DatabaseObjectOperation):
                continue

            key = (app_label, operation.definition.kind, operation.definition.name)
            if operation.removes:
                # Only when what is recorded is what is being removed: a signature change is written as the new
                # overload added up front and the old one removed after the model migrations, both under one name, and
                # the trailing removal must not erase the new definition the leading addition recorded.
                recorded = migrated.get(key)
                if recorded is not None and recorded.definition == operation.definition:
                    migrated.pop(key)
            elif operation.creates:
                migrated[key] = (operation.definition, operation.hints)

    return migrated


def get_declarations():
    """
    Every declaration to manage, from the module named for each kind of object.

    Keyed by (app_label, kind, name), matching the fold over the migration graph. Each kind is read from its own module,
    and a kind whose module path is unset is simply not managed.
    """
    declared = {}

    for kind, declaration_base, get_module in KINDS:
        module_path = get_module()
        if not module_path:
            continue

        for (app_label, name), declaration in get_declared_objects(module_path, kind=declaration_base).items():
            declared[(app_label, kind, name)] = declaration

    return declared


def sides_for(definition, leading, trailing):
    """
    Which of the two lists this kind of object is created on, and which it is dropped on.

    A function is created before its app's model migrations and dropped after them, so that a generated column calling
    it can be added in between. A view is handled opposite, created after model migrations and dropped before them, to
    allow views to reference models. The change table below produces the right ordering for both without knowing which
    kind it is looking at.
    """
    if definition.precedes_models:
        return leading, trailing

    return trailing, leading


def get_object_changes(graph):  # noqa: C901
    """
    Work out the operations needed to bring the migrations in line with the declarations.

    Returns two dicts of operations keyed by app label: the ones that have to run before that app's model migrations,
    and the ones that have to run after them.
    """
    declared = get_declarations()
    migrated = get_migrated_objects(graph)
    migrated_by_db_name = {
        (key[1], definition.db_name): (key, definition, hints) for key, (definition, hints) in migrated.items()
    }
    renamed_keys = set()

    leading = defaultdict(list)
    trailing = defaultdict(list)

    for (app_label, kind, name), declaration in declared.items():
        definition = declaration.definition
        hints = declaration.router_hints
        previous = migrated.get((app_label, kind, name))

        if previous is None:
            # The Python-level key is new, but the database object may not be: a declaration renamed with a pinned
            # db_name is the same object, and treating it as add-new plus remove-old would drop the live one.
            match = migrated_by_db_name.get((kind, definition.db_name))
            if match is not None and match[0] not in declared:
                renamed_keys.add(match[0])
                previous = (match[1], match[2])

        created_on, dropped_on = sides_for(definition, leading, trailing)

        if previous is None:
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
            continue

        previous_definition, previous_hints = previous
        change = definition.plan_change_from(previous_definition)

        if change is Change.UNCHANGED and previous_hints == hints:
            continue

        if change is Change.REPLACE:
            # The old and new cannot coexist and nothing may depend on the object across the change, so both steps
            # happen together rather than straddling the model migrations.
            created_on[app_label].append(definition.remove_operation_class(previous_definition, hints=previous_hints))
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
        elif change is Change.SUPERSEDE or previous_hints != hints:
            # Changed placement puts the object somewhere the old copy is not, so like a changed signature the
            # replacement can be created on its own side and the old copy dropped on the other, leaving the model
            # migrations in between to move every dependent across.
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
            dropped_on[app_label].append(definition.remove_operation_class(previous_definition, hints=previous_hints))
        else:
            created_on[app_label].append(definition.alter_operation_class(definition, previous_definition, hints=hints))

    for key, (definition, hints) in migrated.items():
        if key not in declared and key not in renamed_keys:
            _, dropped_on = sides_for(definition, leading, trailing)
            dropped_on[key[0]].append(definition.remove_operation_class(definition, hints=hints))

    return (
        {app_label: get_ordered_leading(operations) for app_label, operations in leading.items()},
        {app_label: get_ordered_trailing(operations) for app_label, operations in trailing.items()},
    )
