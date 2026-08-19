from django.core.management import call_command, get_commands
from django.test import SimpleTestCase, TestCase
from migration_writing import MigrationWritingMixin


class MakeMigrationsTestCase(MigrationWritingMixin, TestCase):
    """
    End-to-end cover for the wiring: that installing the app is enough for Django's own makemigrations to write the
    object migrations, and that what it writes puts the function in place before the model that uses it.
    """

    app_label = 'example'

    def test_the_function_migration_is_written_in_front_of_the_model_migration(self):
        """
        Case: Run makemigrations for an app whose model has a generated column calling a declared function.
        Expected: Two migrations, the function in the first and the model in the second. The spliced migration is
                  numbered first, so it is renamed to 0001_initial rather than keeping the auto_db_objects placeholder
                  it was built with.
        """
        written = self.make_migrations()

        self.assertEqual(len(written), 2, written)
        self.assertIn('AddFunction', self.read(written[0]))
        self.assertNotIn('AddFunction', self.read(written[1]))
        self.assertIn('CreateModel', self.read(written[1]))

    def test_the_model_migration_depends_on_the_function_migration(self):
        """
        Case: Read the dependencies of the generated model migration.
        Expected: It names the function migration, so the function is guaranteed to exist by the time the column
                  referencing it is created.
        """
        written = self.make_migrations()
        model_migration = self.read(written[1])

        self.assertIn('CreateModel', model_migration)
        self.assertIn("('example', '{}')".format(written[0][:-3]), model_migration)

    def test_the_function_migration_carries_the_definition_rather_than_the_declaration(self):
        """
        Case: Read the generated function migration.
        Expected: It builds a FunctionDefinition with the values spelled out, so editing the declaration later cannot
                  rewrite what has already been applied.
        """
        written = self.make_migrations()
        source = self.read(written[0])

        self.assertIn('postgres_objects.functions.FunctionDefinition', source)
        self.assertNotIn('example.db_functions', source)
        self.assertIn("db_name='example_alluppercase'", source)

    def test_a_second_run_detects_nothing(self):
        """
        Case: Run makemigrations twice with no changes in between.
        Expected: The second run writes nothing, which is what makes --check usable.
        """
        first = self.make_migrations()

        self.assertEqual(self.make_migrations(), first)


class SystemChecksTestCase(SimpleTestCase):
    def test_the_two_commands_agree_on_the_autodetector(self):
        """
        Case: Run the system checks with the app installed.
        Expected: No complaint. Django's commands.E001 refuses to start when makemigrations and migrate name different
                  autodetectors, which is what installing onto both together avoids.
        """
        call_command('check')

    def test_the_commands_are_djangos_own(self):
        """
        Case: Ask which app provides the migration commands.
        Expected: Django's, since this package extends them in place rather than shipping replacements that would take
                  the commands away from whatever else a project installs.
        """
        commands = get_commands()

        self.assertEqual(commands['makemigrations'], 'django.core')
        self.assertEqual(commands['migrate'], 'django.core')
