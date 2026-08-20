from unittest import mock

from django.apps import apps
from django.core.checks import run_checks
from django.db import models
from django.db.models import F
from django.db.models.functions import Upper
from django.test import SimpleTestCase, override_settings
from example.models import Cake

from postgres_objects import Function, GeneratedField, View
from postgres_objects.checks import get_recalculating_fields

APP_LABEL = 'example'


def declare(class_name, base=View, **attrs):
    namespace = {'app_label': APP_LABEL}
    namespace.update(attrs)

    return type(base)(class_name, (base,), namespace)


def declare_function(class_name, **attrs):
    """
    A concrete Function declaration, for the checks that compare a column's dependencies against what is declared.
    """
    namespace = {
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

    return declare(class_name, base=Function, **namespace)


def generated(expression, **kwargs):
    kwargs.setdefault('output_field', models.TextField())
    kwargs.setdefault('db_persist', True)

    return GeneratedField(expression=expression, **kwargs)


class ViewCheckTestCase(SimpleTestCase):
    def run_view_checks(self, *declarations):
        declared = {(APP_LABEL, declaration.name): declaration for declaration in declarations}

        # The example app's generated column will report E003 for every case here, since these settings manage no
        # functions. That check has its own test case below, so we separate out that particular check from other cases.
        with mock.patch('postgres_objects.checks.get_recalculating_fields', return_value=[]):
            with mock.patch('postgres_objects.checks.get_declared_objects', return_value=declared) as collect:
                with override_settings(POSTGRES_OBJECTS={'VIEWS_MODULE_PATH': 'db_views'}):
                    return run_checks(tags=['postgres_objects']), collect

    def test_a_healthy_declaration_raises_no_errors(self):
        """
        Case: Well-formed raw-sql and queryset declarations.
        Expected: No errors.
        """
        errors, _ = self.run_view_checks(
            declare('Uppercased', sql='SELECT id, name FROM example_cake'),
            declare('Counted', queryset=lambda: Cake.objects.values('id', 'name')),
        )

        self.assertEqual(errors, [])

    def test_a_twice_told_body_is_an_error(self):
        """
        Case: A declaration carrying both sql and a queryset.
        Expected: One tagged error naming the declaration (instead of a TypeError inside makemigrations).
        """
        declaration = declare('Uppercased', sql='SELECT 1', queryset=lambda: Cake.objects.values('id'))

        errors, _ = self.run_view_checks(declaration)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, 'postgres_objects.E001')
        self.assertIs(errors[0].obj, declaration)

    def test_an_unbuildable_model_is_an_error(self):
        """
        Case: A queryset declaration whose model has no derivable primary key.
        Expected: A tagged error carrying the explanation, found at check time.
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects.values('name'))

        errors, _ = self.run_view_checks(declaration)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, 'postgres_objects.E002')
        self.assertIn('primary_key', errors[0].msg)

    def test_a_raising_queryset_is_an_error_rather_than_a_crash(self):
        """
        Case: A queryset method that blows up when called.
        Expected: The check reports it against the declaration instead of dying (so every declaration gets looked at).
        """

        def broken():
            raise RuntimeError('models not ready')

        healthy = declare('Counted', queryset=lambda: Cake.objects.values('id'))

        errors, _ = self.run_view_checks(declare('Uppercased', queryset=broken), healthy)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, 'postgres_objects.E001')
        self.assertIn('models not ready', errors[0].msg)

    def test_unmanaged_views_are_not_collected(self):
        """
        Case: A project that never configured VIEWS_MODULE_PATH.
        Expected: No declarations are imported, since neither kind is managed there. The generated column in the example
                  app still reports E003, which is the other check's business and asserted on its own below.
        """
        with mock.patch('postgres_objects.checks.get_declared_objects') as collect:
            with override_settings(POSTGRES_OBJECTS={}):
                errors = run_checks(tags=['postgres_objects'])

        self.assertEqual({error.id for error in errors}, {'postgres_objects.E003'})
        collect.assert_not_called()


class GeneratedFieldCheckTestCase(SimpleTestCase):
    def run_field_checks(self, *fields, declared=(), functions_module='db_functions'):
        collected = {(APP_LABEL, declaration.name): declaration for declaration in declared}
        options = {'FUNCTIONS_MODULE_PATH': functions_module} if functions_module else {}

        with mock.patch('postgres_objects.checks.get_recalculating_fields', return_value=list(fields)):
            with mock.patch('postgres_objects.checks.get_declared_objects', return_value=collected):
                with override_settings(POSTGRES_OBJECTS=options):
                    return run_checks(tags=['postgres_objects'])

    def test_a_column_calling_a_declaration_is_accepted(self):
        """
        Case: An expression calling a declared function and no recalculate_on.
        Expected: No errors.
        """
        doubled = declare_function('Doubled')

        errors = self.run_field_checks(generated(doubled(F('name'))), declared=[doubled])

        self.assertEqual(errors, [])

    def test_a_column_naming_a_declaration_explicitly_is_accepted(self):
        """
        Case: An expression of built-ins only, with the real dependency named through recalculate_on because it is
              called from inside another function's body.
        Expected: No errors.
        """
        doubled = declare_function('Doubled')

        errors = self.run_field_checks(
            generated(Upper(F('name')), recalculate_on=('example_doubled',)), declared=[doubled]
        )

        self.assertEqual(errors, [])

    def test_unmanaged_functions_are_an_error(self):
        """
        Case: The field used in a project that never configured FUNCTIONS_MODULE_PATH.
        Expected: E003.
        """
        errors = self.run_field_checks(generated(Upper(F('name'))), functions_module=None)

        self.assertEqual([error.id for error in errors], ['postgres_objects.E003'])

    def test_an_unresolvable_dependency_is_an_error(self):
        """
        Case: recalculate_on naming something no declaration is called (typo or deleted declaration).
        Expected: E004 naming the offending entry.
        """
        errors = self.run_field_checks(
            generated(Upper(F('name')), recalculate_on=('example_gone',)), declared=[declare_function('Doubled')]
        )

        self.assertEqual([error.id for error in errors], ['postgres_objects.E004'])
        self.assertIn('example_gone', errors[0].msg)

    def test_an_unresolvable_dependency_does_not_also_report_the_general_error(self):
        """
        Case: expression referencing something no declaration is called (typo or deleted declaration).
        Expected: Only E004. (E006 would be true as well, but it says the same thing less usefully.)
        """
        errors = self.run_field_checks(
            generated(Upper(F('name')), recalculate_on=('example_gone',)), declared=[declare_function('Doubled')]
        )

        self.assertEqual([error.id for error in errors], ['postgres_objects.E004'])

    def test_a_virtual_column_is_an_error(self):
        """
        Case: Function on a virtual column.
        Expected: E005.
        """
        doubled = declare_function('Doubled')

        errors = self.run_field_checks(generated(doubled(F('name')), db_persist=False), declared=[doubled])

        self.assertEqual([error.id for error in errors], ['postgres_objects.E005'])

    def test_a_virtual_column_is_an_error_even_without_managed_functions(self):
        """
        Case: A virtual recalculating column, in a project that never configured FUNCTIONS_MODULE_PATH.
        Expected: E005 instead of E003 telling the user to configure a module that still could not make a virtual column
                  recalculate.
        """
        errors = self.run_field_checks(generated(Upper(F('name')), db_persist=False), functions_module=None)

        self.assertEqual([error.id for error in errors], ['postgres_objects.E005'])

    def test_a_column_resolving_nothing_is_an_error(self):
        """
        Case: Expression of built-ins only, with no recalculate_on.
        Expected: E006.
        """
        errors = self.run_field_checks(generated(Upper(F('name'))), declared=[declare_function('Doubled')])

        self.assertEqual([error.id for error in errors], ['postgres_objects.E006'])
        self.assertIn('recalculate=False', errors[0].hint)

    def test_opting_out_is_not_collected_at_all(self):
        """
        Case: A project that opted a column out with recalculate=False.
        Expected: Nothing reported.
        """
        field = GeneratedField(
            expression=Upper(F('name')), output_field=models.TextField(), db_persist=True, recalculate=False
        )

        self.assertEqual(get_recalculating_fields([apps.get_app_config(APP_LABEL)]).count(field), 0)

    def test_the_example_column_is_found_on_its_model(self):
        """
        Case: Collection over the real app registry rather than a patched list.
        Expected: The example app's generated column is found.
        """
        collected = get_recalculating_fields([apps.get_app_config(APP_LABEL)])

        self.assertIn(Cake._meta.get_field('name_uppercased'), collected)
