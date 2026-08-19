"""
What the migrations have created so far, what is declared right now, and the operations that close the gap.
"""

from collections import defaultdict

from postgres_objects.base import Change
from postgres_objects.operations import AddFunction, AlterFunction, DatabaseObjectOperation, RemoveFunction
from postgres_objects.registry import get_declared_objects


def get_ordered_nodes(graph):
    """
    Return every node of the migration graph in an order where a migration always follows the ones it depends on.
    """
    return graph._generate_plan(graph.leaf_nodes(), at_end=True)


def get_migrated_objects(graph):
    """
    Fold the object operations over the migration graph to get what the migrations have created so far.

    Returns a dict keyed by (app_label, name), whose values are the (definition, hints) pair the last operation touching
    that object recorded. An object belongs to the app whose migration created it.
    """
    migrated = {}

    for node in get_ordered_nodes(graph):
        app_label, _ = node

        for operation in graph.nodes[node].operations:
            if not isinstance(operation, DatabaseObjectOperation):
                continue

            key = (app_label, operation.definition.name)
            if isinstance(operation, RemoveFunction):
                migrated.pop(key, None)
            else:
                migrated[key] = (operation.definition, operation.hints)

    return migrated


def get_object_changes(graph, module_path):  # noqa: C901
    """
    Work out the operations needed to bring the migrations in line with the declarations.

    Returns two dicts of operations keyed by app label: the ones that have to run before that app's model migrations,
    and the ones that have to run after them. A generated column's expression calls a function, so the function has to
    exist before the column referencing it is created, and it can only be dropped once nothing references it any more.
    """
    declared = get_declared_objects(module_path)
    migrated = get_migrated_objects(graph)
    migrated_by_db_name = {
        definition.db_name: (key, definition, hints) for key, (definition, hints) in migrated.items()
    }
    renamed_keys = set()

    leading = defaultdict(list)
    trailing = defaultdict(list)

    for (app_label, name), declaration in declared.items():
        definition = declaration.definition
        hints = declaration.router_hints
        previous = migrated.get((app_label, name))

        if previous is None:
            # The Python-level key is new, but the database object may not be: a declaration renamed with a pinned
            # db_name is the same object, and treating it as add-new plus remove-old would drop the live one.
            match = migrated_by_db_name.get(definition.db_name)
            if match is not None and match[0] not in declared:
                renamed_keys.add(match[0])
                previous = (match[1], match[2])

        if previous is None:
            leading[app_label].append(AddFunction(definition, hints=hints))
            continue

        previous_definition, previous_hints = previous
        change = definition.plan_change_from(previous_definition)

        if change is Change.UNCHANGED and previous_hints == hints:
            continue

        if change is Change.REPLACE:
            leading[app_label].append(RemoveFunction(previous_definition, hints=previous_hints))
            leading[app_label].append(AddFunction(definition, hints=hints))
        elif change is Change.SUPERSEDE or previous_hints != hints:
            # Changed placement puts the object somewhere the old copy is not, so like a changed signature the
            # replacement can be created up front and the old copy dropped only after the model migrations have moved
            # every dependent onto it.
            leading[app_label].append(AddFunction(definition, hints=hints))
            trailing[app_label].append(RemoveFunction(previous_definition, hints=previous_hints))
        else:
            leading[app_label].append(AlterFunction(definition, previous_definition, hints=hints))

    for (app_label, name), (definition, hints) in migrated.items():
        if (app_label, name) not in declared and (app_label, name) not in renamed_keys:
            trailing[app_label].append(RemoveFunction(definition, hints=hints))

    return dict(leading), dict(trailing)
