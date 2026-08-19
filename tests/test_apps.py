from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings


class SettingsValidationTestCase(SimpleTestCase):
    def ready(self):
        apps.get_app_config('postgres_objects').ready()

    def test_a_valid_configuration_is_accepted(self):
        """
        Case: The setting names a module path, as configured for this project.
        Expected: No error.
        """
        self.ready()

    @override_settings(POSTGRES_OBJECTS={})
    def test_an_absent_functions_module_is_accepted(self):
        """
        Case: The settings dict names no functions module.
        Expected: No error (functions module is considered disabled).
        """
        self.ready()

    @override_settings(POSTGRES_OBJECTS='db_functions')
    def test_a_setting_that_is_not_a_dict_is_refused(self):
        """
        Case: The whole setting given as a bare string rather than a dict.
        Expected: ImproperlyConfigured at startup.
        """
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.ready()

        self.assertIn('must be a dict', str(caught.exception))

    @override_settings(POSTGRES_OBJECTS={'FUNCTIONS_MODULE_PATH': ['db_functions']})
    def test_a_functions_module_that_is_not_a_string_is_refused(self):
        """
        Case: The functions module given as a list rather than a module path.
        Expected: ImproperlyConfigured naming what the value should be.
        """
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.ready()

        self.assertIn('module path', str(caught.exception))
