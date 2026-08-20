"""
What the migrations have created so far, what is declared right now, and the operations that close the gap.
"""

from collections import defaultdict, namedtuple

from postgres_objects.autodetector.ordering import get_ordered_leading, get_ordered_trailing
from postgres_objects.autodetector.recalculation import get_recalculations
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

#: What the migrations have recorded about one object: its definition, the router hints it was created with, and the
#: node that last created it. The node is what turns a reference to an object an earlier run created into a migration
#: to wait for.
MigratedObject = namedtuple('MigratedObject', ('definition', 'hints', 'node'))


def get_ordered_nodes(graph):
    """
    Return every node of the migration graph in an order where a migration always follows the ones it depends on.
    """
    return graph._generate_plan(graph.leaf_nodes(), at_end=True)


def get_migrated_objects(graph):
    """
    Fold the object operations over the migration graph to get what the migrations have created so far.

    Returns a dict keyed by (app_label, kind, name), whose values are the MigratedObject the last operation touching
    that object recorded. An object belongs to the app whose migration created it.
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
                # An alter written for a pinned-db_name rename carries the previous definition under its old name.
                # That old key is this very object and has to be retired with the alter, or the fold would report the
                # object twice and the sweep would drop the live identifier under its old name.
                previous = getattr(operation, 'previous', None)
                if previous is not None and previous.name != operation.definition.name:
                    migrated.pop((app_label, operation.definition.kind, previous.name), None)

                migrated[key] = MigratedObject(operation.definition, operation.hints, node)

    return migrated


def get_configured_kinds():
    """
    The kinds whose settings name a module to read declarations from. A kind whose module path is unset is not managed
    in either direction: nothing of that kind is declared, and nothing of it is swept.
    """
    return {kind for kind, _, get_module in KINDS if get_module()}


def get_declarations():
    """
    Every declaration to manage, from the module named for each kind of object.

    Keyed by (app_label, kind, name), matching the fold over the migration graph. Each kind is read from its own module,
    and a kind whose module path is unset is simply not managed.
    """
    declared = {}
    configured = get_configured_kinds()

    for kind, declaration_base, get_module in KINDS:
        if kind not in configured:
            continue

        for (app_label, name), declaration in get_declared_objects(get_module(), kind=declaration_base).items():
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


def get_object_changes(graph, from_state=None, to_state=None):  # noqa: C901
    """
    Work out the operations needed to bring the migrations in line with the declarations.

    Returns three dicts:
    1. Operations, keyed by app label, that have to run before that app's model migrations,
    2. Operations, keyed by app label, that have to run after them,
    3. Other-app migration nodes each app's leading operations rework the objects of.

    A leading operation built against an object another app's migration created has to run after that creator, and only
    the fold knows which node that was. Given the project states the autodetector compares, the trailing side also
    carries a recalculation for every stored generated column that opted in and depends on a function whose computed
    values change; without the states that part would simply be skipped.
    """
    declared = get_declarations()
    migrated = get_migrated_objects(graph)
    migrated_by_db_name = defaultdict(list)
    for key, migrated_object in migrated.items():
        migrated_by_db_name[(key[1], migrated_object.definition.db_name)].append((key, migrated_object))
    renamed_keys = set()
    declared_identifiers = set()
    recalculable = set()

    leading = defaultdict(list)
    trailing = defaultdict(list)
    creators = defaultdict(set)

    for (app_label, kind, name), declaration in declared.items():
        definition = declaration.definition
        hints = declaration.router_hints
        declared_identifiers.add((kind, definition.db_name))
        previous = migrated.get((app_label, kind, name))

        if previous is None:
            # The Python-level key is new, but the database object may not be: a declaration renamed with a pinned
            # db_name is the same object, and treating it as add-new plus remove-old would drop the live one. The
            # declaring app's own record is preferred, since two apps' migrations may have created the identifier
            # each on their own database, and matching across apps would rename the wrong record.
            candidates = [
                candidate
                for candidate in migrated_by_db_name.get((kind, definition.db_name), [])
                if candidate[0] not in declared
            ]
            match = next(
                (candidate for candidate in candidates if candidate[0][0] == app_label),
                candidates[-1] if candidates else None,
            )
            if match is not None:
                renamed_keys.add(match[0])
                previous = match[1]

        created_on, dropped_on = sides_for(definition, leading, trailing)

        if previous is None:
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
            continue

        previous_definition, previous_hints = previous.definition, previous.hints
        change = definition.plan_change_from(previous_definition)

        # Every shape of change can leave stale values behind. A REPLACE's drop is blocked only by directly dependent
        # columns (one depending through recalculate_on will be forgotten) and a SUPERSEDE rewrites a column only when
        # its expression changes too, which an argument gaining a default does not. The recalculation is also what moves
        # a column across a supersede: SET EXPRESSION re-resolves the expression, rebinding the column onto the new
        # overload, and it runs at the head of the trailing side, ahead of the drop it unblocks.
        if kind == 'function' and definition.alters_computed_values_from(previous_definition):
            recalculable.add(definition.db_name.lower())

        if change is Change.UNCHANGED and previous_hints == hints:
            continue

        if created_on is leading and previous.node[0] != app_label:
            # The rework lands ahead of this app's model migrations, but the object it starts from was created by
            # another app's migration. Nothing else orders the two apps on a fresh database, and applying the rework
            # first would let the creator's CREATE OR REPLACE quietly restore the old definition.
            creators[app_label].add(previous.node)

        if change is Change.SUPERSEDE:
            # A changed identity coexists with the old copy as an overload, so the replacement can be created on its
            # own side and the old copy dropped on the other, leaving the model migrations in between to move every
            # dependent across.
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
            dropped_on[app_label].append(definition.remove_operation_class(previous_definition, hints=previous_hints))
        elif change is Change.REPLACE or (previous_hints != hints and definition.precedes_models):
            # The old and new cannot coexist, so both steps happen together rather than straddling the model
            # migrations. A placement change of an object whose create leads takes the same shape: straddled, its drop
            # would trail, and on any connection both hint sets allow that trailing drop names the very object the
            # leading create just wrote. Adjacent and drop-first, the pair is safe on every connection: both hint sets
            # allowed recreates, only the new one creates, only the old one drops.
            created_on[app_label].append(definition.remove_operation_class(previous_definition, hints=previous_hints))
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
        elif previous_hints != hints:
            # A placement change of a view. Its drop side runs before its create side, so the straddle is safe here,
            # and it keeps the model migrations running with the view out of the way.
            created_on[app_label].append(definition.add_operation_class(definition, hints=hints))
            dropped_on[app_label].append(definition.remove_operation_class(previous_definition, hints=previous_hints))
        else:
            created_on[app_label].append(definition.alter_operation_class(definition, previous_definition, hints=hints))

    configured = get_configured_kinds()

    for key, migrated_object in migrated.items():
        if key[1] not in configured:
            # An unmanaged kind. Its declarations were never read, so a migrated object of it missing from `declared`
            # says nothing about deletion, and sweeping it would drop every object of the kind at once.
            continue

        if key not in declared and key not in renamed_keys:
            definition = migrated_object.definition
            if (key[1], definition.db_name) in declared_identifiers:
                # A live declaration owns this identifier, so whatever bookkeeping left the stale key behind, a DROP
                # naming it would take the declared object down with it. The entry is left alone instead.
                continue

            _, dropped_on = sides_for(definition, leading, trailing)
            dropped_on[key[0]].append(definition.remove_operation_class(definition, hints=migrated_object.hints))

    recalculations = get_recalculations(recalculable, from_state, to_state)

    return (
        {app_label: get_ordered_leading(operations) for app_label, operations in leading.items()},
        # The recalculations come first on the trailing side: a create that trails (e.g. a materialized view) snapshots
        # data, so it has to see recomputed values. They stay out of get_ordered_trailing, which orders declared objects
        # and rightly knows nothing about model-bound operations.
        {
            app_label: recalculations.get(app_label, []) + get_ordered_trailing(trailing.get(app_label, []))
            for app_label in set(trailing) | set(recalculations)
        },
        dict(creators),
    )
