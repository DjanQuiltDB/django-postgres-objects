from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from postgres_objects import Function
from postgres_objects.registry import (
    get_declared_objects,
    get_functions_module,
    get_views_module,
    import_app_module,
)


class ImportAppModuleTestCase(SimpleTestCase):
    def test_it_imports_a_module_from_within_an_app(self):
        """
        Case: An app that has the named module.
        Expected: That app's copy of it, resolved relative to the app rather than the project root.
        """
        module = import_app_module(apps.get_app_config('example'), 'db_functions')

        self.assertEqual(module.__name__, 'example.db_functions')

    def test_a_dotted_path_works(self):
        """
        Case: A module path naming a submodule of a package inside the app.
        Expected: Resolved the same way, so declarations can live in a package rather than one file.
        """
        module = import_app_module(apps.get_app_config('example'), 'db.functions')

        self.assertEqual(module.__name__, 'example.db.functions')

    def test_an_app_without_the_module_is_not_an_error(self):
        """
        Case: An app that simply does not declare anything.
        Expected: None, since that is the normal case rather than a misconfiguration.
        """
        self.assertIsNone(import_app_module(apps.get_app_config('auth'), 'db_functions'))

    def test_a_broken_import_inside_the_module_propagates(self):
        """
        Case: The functions module exists but imports something that does not.
        Expected: The error is raised rather than being read as "nothing declared here".
        """
        with self.assertRaises(ModuleNotFoundError) as caught:
            import_app_module(apps.get_app_config('example'), 'broken_functions')

        self.assertEqual(caught.exception.name, 'a_module_that_does_not_exist')


class GetDeclaredObjectsTestCase(SimpleTestCase):
    def test_it_collects_the_declarations_of_every_app(self):
        """
        Case: Collect from the configured functions module.
        Expected: Each declaration keyed by the app that owns it and its name, which is the key the migration graph is
                  folded into.
        """
        declared = get_declared_objects('db_functions')

        self.assertIn(('example', 'alluppercase'), declared)
        self.assertEqual(declared[('example', 'alluppercase')].resolved_db_name, 'example_alluppercase')

    def test_abstract_declarations_are_skipped(self):
        """
        Case: A functions module that imports an abstract declaration and subclasses it.
        Expected: Only the concrete subclass is collected, so a shared base is never mistaken for an object.
        """
        declared = get_declared_objects('more_functions')

        self.assertNotIn(('example', 'reusablebody'), declared)
        self.assertIn(('example', 'inheritedbody'), declared)

    def test_the_imported_base_class_is_not_collected(self):
        """
        Case: A functions module that imports Function itself in order to subclass it.
        Expected: Function is abstract, so importing it declares nothing.
        """
        declared = get_declared_objects('db_functions')

        self.assertNotIn(('postgres_objects', 'function'), declared)
        self.assertEqual([key for key in declared if key[1] == 'function'], [])

    def test_two_declarations_of_the_same_name_in_one_app_are_refused(self):
        """
        Case: One app declaring two different objects under the same name.
        Expected: ImproperlyConfigured, since one would otherwise silently shadow the other in the change detection.
        """
        declarations = {
            'First': type(Function)('First', (Function,), {'app_label': 'example', 'name': 'clash', 'returns': 'TEXT'}),
            'Second': type(Function)(
                'Second', (Function,), {'app_label': 'example', 'name': 'clash', 'returns': 'INT'}
            ),
        }

        with self.assertRaises(ImproperlyConfigured) as caught:
            self._collect(declarations)

        self.assertIn('two different objects', str(caught.exception))

    def test_two_declarations_landing_on_one_identifier_are_refused(self):
        """
        Case: Two differently named declarations pinned to the same db_name.
        Expected: ImproperlyConfigured naming the way out, since the two would overwrite each other in the database.
        """
        declarations = {
            'First': type(Function)(
                'First', (Function,), {'app_label': 'example', 'name': 'a', 'db_name': 'same', 'returns': 'TEXT'}
            ),
            'Second': type(Function)(
                'Second', (Function,), {'app_label': 'example', 'name': 'b', 'db_name': 'same', 'returns': 'INT'}
            ),
        }

        with self.assertRaises(ImproperlyConfigured) as caught:
            self._collect(declarations)

        self.assertIn('Set db_name', str(caught.exception))

    def _collect(self, declarations):
        """
        Run the collector over a throwaway module holding the given declarations.
        """
        import sys
        import types

        module = types.ModuleType('example.throwaway_functions')
        for attribute, declaration in declarations.items():
            setattr(module, attribute, declaration)

        sys.modules['example.throwaway_functions'] = module
        self.addCleanup(sys.modules.pop, 'example.throwaway_functions', None)

        return get_declared_objects('throwaway_functions')


class FunctionsModuleSettingTestCase(SimpleTestCase):
    def test_it_reads_the_setting(self):
        """
        Case: The functions module is configured.
        Expected: Its path is returned, which is what switches the whole feature on.
        """
        self.assertEqual(get_functions_module(), 'db_functions')

    @override_settings(POSTGRES_OBJECTS={})
    def test_an_unset_module_switches_the_feature_off(self):
        """
        Case: The settings dict names no functions module.
        Expected: None, so makemigrations behaves exactly as stock Django.
        """
        self.assertIsNone(get_functions_module())

    @override_settings()
    def test_the_whole_setting_may_be_absent(self):
        """
        Case: A project that never configured this package at all.
        Expected: None rather than an AttributeError, so installing the app is harmless on its own.
        """
        from django.conf import settings

        del settings.POSTGRES_OBJECTS

        self.assertIsNone(get_functions_module())


class ViewsModuleSettingTestCase(SimpleTestCase):
    def test_it_reads_its_own_key(self):
        """
        Case: The views module is configured.
        Expected: Its path is returned, separately from the functions one, so each kind is declared in its own module.
        """
        self.assertEqual(get_views_module(), 'db_views')

    @override_settings(POSTGRES_OBJECTS={'FUNCTIONS_MODULE_PATH': 'db_functions'})
    def test_managing_functions_alone_leaves_views_unmanaged(self):
        """
        Case: A project naming only the functions module.
        Expected: None for views, so adopting the second kind of object is opt-in.
        """
        self.assertIsNone(get_views_module())
        self.assertEqual(get_functions_module(), 'db_functions')
