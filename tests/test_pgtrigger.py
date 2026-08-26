"""
Compatibility with django-pgtrigger, which extends the same two commands this package does.

Only meaningful with pgtrigger installed and listed in INSTALLED_APPS, which is the py314-dj60-pgtrigger environment and
nowhere else; the rest of the suite skips these rather than depending on the library.
"""

from unittest import skipUnless

from django.apps import apps
from django.core.management import call_command
from django.core.management.commands import makemigrations, migrate
from django.test import SimpleTestCase, TestCase
from migration_writing import MigrationWritingMixin

from postgres_objects.autodetector import DeclarativeObjectAutodetectorMixin, get_autodetector

try:
    from pgtrigger.migrations import MigrationAutodetectorMixin as PgtriggerMixin
except ImportError:  # pragma: no cover - the ordinary environments, where pgtrigger is absent
    PgtriggerMixin = None

INSTALLED = PgtriggerMixin is not None and apps.is_installed('pgtrigger')
REASON = 'django-pgtrigger is not installed'


@skipUnless(INSTALLED, REASON)
class AutodetectorTestCase(SimpleTestCase):
    def test_one_autodetector_carries_both_libraries(self):
        """
        Case: Ask for the autodetector with both apps installed.
        Expected: One class carrying both libraries' autodetection, whichever of the two was readied first.
        """
        autodetector = get_autodetector()

        self.assertTrue(issubclass(autodetector, DeclarativeObjectAutodetectorMixin))
        self.assertTrue(issubclass(autodetector, PgtriggerMixin))

    def test_both_commands_run_it(self):
        """
        Case: Read the autodetector off each migration command.
        Expected: The same class on both, which is what Django's commands.E001 insists on. pgtrigger assigns both
                  command attributes from its own composition, so this also covers this package surviving that.
        """
        autodetector = get_autodetector()

        self.assertIs(makemigrations.Command.autodetector, autodetector)
        self.assertIs(migrate.Command.autodetector, autodetector)

    def test_neither_library_is_left_out_of_the_module_level_names(self):
        """
        Case: Read the names each library composes onto when it patches.
        Expected: Both carry both libraries. A library patching later builds on these rather than on the command
                  attribute, so a contribution missing here is one that would be dropped by the next patcher.
        """
        for name in (makemigrations.MigrationAutodetector, migrate.MigrationAutodetector):
            self.assertTrue(issubclass(name, DeclarativeObjectAutodetectorMixin), name)
            self.assertTrue(issubclass(name, PgtriggerMixin), name)

    def test_the_system_checks_are_happy(self):
        """
        Case: Run the system checks with both libraries installed.
        Expected: No complaint, in particular no commands.E001 about the two commands disagreeing.
        """
        call_command('check')


@skipUnless(INSTALLED, REASON)
class MakeMigrationsTestCase(MigrationWritingMixin, TestCase):
    """
    A database is needed only because makemigrations checks the recorded history against it before writing.
    """

    app_label = 'pgtrigger_example'

    def test_one_run_writes_both_libraries_operations(self):
        """
        Case: Run makemigrations for an app declaring a function and a model that carries both a generated column
              calling it and a pgtrigger trigger.
        Expected: The function migration in front, and the model migration carrying both the model and its trigger.
                  Whichever library detects nothing is the one being shadowed.
        """
        written = self.make_migrations()

        self.assertEqual(len(written), 2, written)
        self.assertIn('AddFunction', self.read(written[0]))

        model_migration = self.read(written[1])
        self.assertIn('CreateModel', model_migration)
        self.assertIn('AddTrigger', model_migration)

    def test_the_model_migration_still_depends_on_the_function_migration(self):
        """
        Case: Read the dependencies of the model migration written alongside a trigger.
        Expected: It names the function migration, so the ordering this package arranges is not disturbed by the
                  operations the other library adds.
        """
        written = self.make_migrations()

        self.assertIn("('pgtrigger_example', '{}')".format(written[0][:-3]), self.read(written[1]))
