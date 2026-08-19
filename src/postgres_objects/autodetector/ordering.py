"""
Ordering the object operations within one app's leading and trailing migrations.
"""


def get_split_by_placement(operations):
    """
    Separate operations on objects that exist after the model migrations from those that exist before them.
    """
    after = [operation for operation in operations if not operation.definition.precedes_models]
    before = [operation for operation in operations if operation.definition.precedes_models]

    return after, before


def get_ordered_leading(operations):
    """
    Views first, then functions.

    Everything here runs before the model migrations, and the two kinds nest: a view has to be gone before anything it
    reads is touched, including a function it calls. The views are all drops, so declaration order is reversed to drop a
    dependent view before the one it selects from.
    """
    views, functions = get_split_by_placement(operations)
    views.reverse()

    return views + functions


def get_ordered_trailing(operations):
    """
    Functions first, then views, mirroring get_ordered_leading.

    The views here are all creates, so they keep declaration order: a view declared below the one it selects from is
    created after it.
    """
    views, functions = get_split_by_placement(operations)

    return functions + views
