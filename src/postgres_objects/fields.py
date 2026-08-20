"""
The model field that ties a stored generated column to the postgres_objects.Function declarations it computes with.
"""

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Func

from postgres_objects.functions import Function


def resolve_dependency(dependency):
    """
    The database name a ``recalculate_on`` entry refers to.

    A postgres_objects.Function declaration is accepted for readability and normalized here; a plain string is taken as
    the database name itself. Anything else is refused outright rather than left to fail as an AttributeError further
    along, because mistakes like passing a Django expression or a function's *call* instead of the declaration would
    otherwise look like they worked.
    """
    if isinstance(dependency, str):
        return dependency

    if isinstance(dependency, type) and issubclass(dependency, Function):
        if dependency.abstract:
            raise ImproperlyConfigured(
                '{} is abstract, so it names no function to recalculate on.'.format(dependency.__name__)
            )

        return dependency.resolved_db_name

    raise ImproperlyConfigured(
        'recalculate_on takes postgres_objects.Function declarations or their database names as strings, not '
        '{!r}.'.format(dependency)
    )


class GeneratedField(models.GeneratedField):
    """
    A GeneratedField whose stored values are recalculated when a :class:`~postgres_objects.functions.Function`
    declaration it uses changes.

    PostgreSQL computes a stored generated column when a row is written, not when the function behind it changes, so
    replacing a function's body leaves every existing row holding a value the old body produced. Declaring the column
    with this field makes the autodetector notice such a change and write a
    :class:`~postgres_objects.operations.RecalculateGeneratedField` operation for it.

    The declarations the expression calls directly are found by walking it. One that is only called from inside another
    function's body cannot be seen that way; name it in ``recalculate_on``, as either the declaration class or its
    database name. Built-ins are found by the same walk and simply resolve to nothing, which is harmless.

    A column that resolves no ``Function`` declaration at all cannot recalculate, so this field would be promising
    something it can never deliver. The system checks refuse that, and ``recalculate=False`` is how a project says it
    wants the wrapper anyway and accepts that nothing will be recomputed.
    """

    def __init__(self, *, recalculate=True, recalculate_on=(), **kwargs):
        if not recalculate and recalculate_on:
            raise ImproperlyConfigured(
                'recalculate_on names functions to recalculate on, which recalculate=False switches off. Drop one of '
                'the two.'
            )

        self.recalculate = recalculate

        # Normalized to database names right away: the field is serialized into migrations through deconstruct(), and a
        # migration must never import a declaration, or deleting the declaration would break history.
        self.recalculate_on = tuple(resolve_dependency(dependency) for dependency in recalculate_on)
        super().__init__(**kwargs)

    def deconstruct(self):
        # ModelState keeps clones of the real fields, and Field.clone() round-trips through deconstruct(), so anything
        # not written here is silently lost from the migration state. For recalculate that would mean an opted-out
        # column recalculating after all.
        name, path, args, kwargs = super().deconstruct()
        if not self.recalculate:
            kwargs['recalculate'] = False
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
