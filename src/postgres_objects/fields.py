"""
The model field that ties a stored generated column to the declared functions it computes with.
"""

from django.db import models
from django.db.models import Func


class GeneratedField(models.GeneratedField):
    """
    A GeneratedField whose stored values are recalculated when a declared function it uses changes.

    PostgreSQL computes a stored generated column when a row is written, not when the function behind it changes, so
    replacing a function's body leaves every existing row holding a value the old body produced. Declaring the column
    with this field makes the autodetector notice such a change and write a
    :class:`~postgres_objects.operations.RecalculateGeneratedField` operation for it.

    The functions the expression calls directly are found by walking it. A function that is only called from inside
    another function's body cannot be seen that way; name it in ``recalculate_on``, as either the declaration class or
    its database name.
    """

    def __init__(self, *, recalculate_on=(), **kwargs):
        # Normalized to database names right away: the field is serialized into migrations through deconstruct(), and a
        # migration must never import a declaration, or deleting the declaration would break history.
        self.recalculate_on = tuple(
            dependency if isinstance(dependency, str) else dependency.resolved_db_name for dependency in recalculate_on
        )
        super().__init__(**kwargs)

    def deconstruct(self):
        # ModelState keeps clones of the real fields, and Field.clone() round-trips through deconstruct(), so anything
        # not written here is silently lost from the migration state.
        name, path, args, kwargs = super().deconstruct()
        if self.recalculate_on:
            kwargs['recalculate_on'] = self.recalculate_on
        return name, path, args, kwargs

    def referenced_function_names(self):
        """
        The database names of every function this column depends on, lowercased.

        The expression contributes each Func node's function name; declarations produce such nodes carrying their
        resolved_db_name, and a built-in like UPPER contributes a name no declaration can resolve to, which is harmless.
        recalculate_on is added on top.
        """
        names = {name.lower() for name in self.recalculate_on}

        nodes = self.expression.flatten() if hasattr(self.expression, 'flatten') else (self.expression,)
        for node in nodes:
            if isinstance(node, Func):
                function = node.extra.get('function', node.function)
                if function:
                    names.add(function.lower())

        return names
