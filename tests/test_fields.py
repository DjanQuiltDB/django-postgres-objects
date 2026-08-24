from django.db import models
from django.db.models import F, Value
from django.db.models.functions import Concat, Upper
from django.test import SimpleTestCase

from postgres_objects import Function, GeneratedField


def declare(class_name, **attrs):
    """
    Build a declaration on the fly, so a test can vary one attribute without a fixture module per case.
    """
    namespace = {
        'app_label': 'example',
        'arguments': 'input TEXT',
        'returns': 'TEXT',
        'body': """
            BEGIN
                RETURN input;
            END;
        """,
        'output_field': models.TextField(),
    }
    namespace.update(attrs)

    return type(Function)(class_name, (Function,), namespace)


def field(expression, **kwargs):
    kwargs.setdefault('output_field', models.TextField())
    kwargs.setdefault('db_persist', True)

    return GeneratedField(expression=expression, **kwargs)


class DeconstructTestCase(SimpleTestCase):
    def test_it_deconstructs_to_its_own_path(self):
        """
        Case: The wrapper field, deconstructed as the migration writer would.
        Expected: The path names this package, not django.db.models, so the marker survives in written migrations and
                  therefore in the historical project state.
        """
        _, path, _, _ = field(Upper(F('name'))).deconstruct()

        self.assertEqual(path, 'postgres_objects.fields.GeneratedField')

    def test_recalculate_on_survives_a_clone(self):
        """
        Case: A field carrying an explicit dependency, cloned the way ModelState clones every field.
        Expected: The clone still carries the dependency.
        """
        clone = field(Upper(F('name')), recalculate_on=('example_doubled',)).clone()

        self.assertEqual(clone.recalculate_on, ('example_doubled',))

    def test_recalculate_on_is_omitted_when_empty(self):
        """
        Case: A field with no explicit dependencies.
        Expected: deconstruct() leaves the kwarg out.
        """
        _, _, _, kwargs = field(Upper(F('name'))).deconstruct()

        self.assertNotIn('recalculate_on', kwargs)


class RecalculateOnTestCase(SimpleTestCase):
    def test_declarations_are_normalized_to_db_names(self):
        """
        Case: recalculate_on given as a declaration class rather than a string.
        Expected: Stored as the resolved database name.
        """
        self.assertEqual(
            field(Upper(F('name')), recalculate_on=(declare('Doubled'),)).recalculate_on,
            ('example_doubled',),
        )

    def test_strings_pass_through(self):
        """
        Case: recalculate_on given as a plain database name.
        Expected: Stored as provided.
        """
        self.assertEqual(
            field(Upper(F('name')), recalculate_on=('example_doubled',)).recalculate_on, ('example_doubled',)
        )


class ReferencedFunctionNamesTestCase(SimpleTestCase):
    def test_it_finds_a_declared_function_call(self):
        """
        Case: An expression calling a declared function, the way FunctionMeta.__call__ builds the call.
        Expected: The function's database name is reported, lowercased.
        """
        doubled = declare('Doubled')

        self.assertIn('example_doubled', field(doubled(F('name'))).referenced_function_names())

    def test_it_finds_calls_nested_inside_other_expressions(self):
        """
        Case: The declared function call buried inside a Concat.
        Expected: The function reference is resolved.
        """
        doubled = declare('Doubled')

        names = field(Concat(Value('a'), doubled(F('name')))).referenced_function_names()

        self.assertIn('example_doubled', names)

    def test_explicit_dependencies_are_included(self):
        """
        Case: A non-detectable function is named through recalculate_on aside from detectable functions in the
              expression.
        Expected: The function is reported alongside the ones found in the expression.
        """
        doubled = declare('Doubled')

        names = field(doubled(F('name')), recalculate_on=('example_tripled',)).referenced_function_names()

        self.assertEqual({'example_doubled', 'example_tripled'}, names & {'example_doubled', 'example_tripled'})

    def test_an_expression_without_function_calls_reports_nothing(self):
        """
        Case: A generation expression that is a bare column reference.
        Expected: No names, and no crash on an expression that is not a Func.
        """
        self.assertEqual(field(F('name')).referenced_function_names(), set())
