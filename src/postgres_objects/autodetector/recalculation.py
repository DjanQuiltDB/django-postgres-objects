"""
Recalculating stored generated columns whose functions change computed values.
"""

from collections import defaultdict

from postgres_objects.fields import GeneratedField
from postgres_objects.operations import RecalculateGeneratedField


def get_recalculations(function_names, from_state, to_state):
    """
    A recalculation for every stored generated column that opted in through ``postgres_objects.GeneratedField`` and
    depends on a declared named function.

    The column has to exist on both sides of the change: one being added right now is computed with the new body
    already, and one being removed leaves nothing to recompute. Each operation lands in its model's app, whose trailing
    object migration waits for every other app's object migrations, so the rewrite always follows the function change
    regardless of where the two live.
    """
    operations = defaultdict(list)

    if not function_names or from_state is None or to_state is None:
        return operations

    for (app_label, model_name), model_state in sorted(to_state.models.items()):
        for field_name, field in model_state.fields.items():
            if not isinstance(field, GeneratedField) or not field.db_persist or not field.recalculate:
                continue

            if not field.referenced_function_names() & function_names:
                continue

            previous_model = from_state.models.get((app_label, model_name))
            previous_field = previous_model.fields.get(field_name) if previous_model else None
            if previous_field is None or not getattr(previous_field, 'generated', False):
                continue

            operations[app_label].append(RecalculateGeneratedField(model_state.name, field_name))

    return operations
