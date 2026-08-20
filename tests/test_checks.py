from unittest import mock

from django.core.checks import run_checks
from django.test import SimpleTestCase, override_settings
from example.models import Cake

from postgres_objects import View

APP_LABEL = 'example'


def declare(class_name, base=View, **attrs):
    namespace = {'app_label': APP_LABEL}
    namespace.update(attrs)

    return type(base)(class_name, (base,), namespace)


class ViewCheckTestCase(SimpleTestCase):
    def run_view_checks(self, *declarations):
        declared = {(APP_LABEL, declaration.name): declaration for declaration in declarations}

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
        Expected: The check touches nothing, since views are simply not managed there.
        """
        with mock.patch('postgres_objects.checks.get_declared_objects') as collect:
            with override_settings(POSTGRES_OBJECTS={}):
                errors = run_checks(tags=['postgres_objects'])

        self.assertEqual(errors, [])
        collect.assert_not_called()
