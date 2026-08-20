import warnings
from unittest import mock

from django.core.management import call_command, get_commands
from django.test import SimpleTestCase, TestCase, override_settings
from example.db_functions import AllUppercase
from migration_writing import MigrationWritingMixin

from postgres_objects.autodetector import UnmigratedAppWarning


class MakeMigrationsTestCase(MigrationWritingMixin, TestCase):
    """
    End-to-end cover for the wiring: that installing the app is enough for Django's own makemigrations to write the
    object migrations, and that what it writes puts the function in place before the model that uses it.
    """

    app_label = 'example'

    def test_the_function_migration_is_written_in_front_of_the_model_migration(self):
        """
        Case: Run makemigrations for an app whose model has a generated column calling a declared function.
        Expected: The function comes in the first migration and the model in the second. The spliced migration is
                  numbered first, so it is renamed to 0001_initial rather than keeping the auto_db_objects placeholder
                  it was built with.
        """
        written = self.make_migrations()

        self.assertIn('AddFunction', self.read(written[0]))
        self.assertNotIn('AddFunction', self.read(written[1]))
        self.assertIn('CreateModel', self.read(written[1]))

    def test_the_view_migration_is_written_behind_the_model_migration(self):
        """
        Case: Run makemigrations for an app declaring both functions and views.
        Expected: Three migrations, with the views last. That is the ordering inversion: a function has to exist before
                  the column calling it, while a view can only be created once the table it selects from is there.
        """
        written = self.make_migrations()

        self.assertEqual(len(written), 3, written)
        self.assertNotIn('AddView', self.read(written[0]))
        self.assertNotIn('AddView', self.read(written[1]))
        self.assertIn('AddView', self.read(written[2]))
        self.assertIn('CreateModel', self.read(written[1]))

    def test_a_view_is_created_after_the_view_it_selects_from(self):
        """
        Case: Read the generated view migration, which holds a view built on another one.
        Expected: The one being selected from comes first, since declaration order is what decides the order they are
                  created in.
        """
        written = self.make_migrations()
        source = self.read(written[2])

        self.assertLess(source.index("name='uppercasedcakes'"), source.index("name='stackedcakes'"))

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

    def test_a_queryset_view_migration_is_self_contained(self):
        """
        Case: Read the generated view migration, which includes a view declared as a queryset.
        Expected: The compiled SELECT spelled out and no reference to the declaring module, its models or the queryset,
                  so the migration means the same thing however the declaration changes later.
        """
        written = self.make_migrations()
        source = self.read(written[2])

        self.assertIn("name='cakecounts'", source)
        self.assertIn('"example_cake"."name"', source)
        self.assertNotIn('example.db_views', source)
        self.assertNotIn('queryset', source)

    def test_a_body_change_writes_a_recalculation_behind_the_alteration(self):
        """
        Case: Edit the body of the function the example model's generated column calls, and run makemigrations again.
        Expected: An AlterFunction migration plus a trailing migration recalculating the column, the latter depending on
                  the former, so the rewrite always sees the new body.
        """
        first = self.make_migrations()

        with mock.patch.object(AllUppercase, 'body', 'BEGIN RETURN LOWER(input); END;'):
            written = self.make_migrations()

        new = [name for name in written if name not in first]
        self.assertEqual(len(new), 2, new)

        alteration, recalculation = (self.read(name) for name in new)
        self.assertIn('AlterFunction', alteration)
        self.assertIn('RecalculateGeneratedField', recalculation)
        self.assertIn("model_name='Cake'", recalculation)
        self.assertIn("name='name_uppercased'", recalculation)
        self.assertIn("('example', '{}')".format(new[0][:-3]), recalculation)


class CrossAppMakeMigrationsTestCase(MigrationWritingMixin, TestCase):
    app_label = 'bakery'
    also_migrate = ('example',)

    def test_a_joining_view_carries_every_table_it_reads(self):
        """
        Case: Read the migration for a view whose queryset traverses relations into another app.
        Expected: The compiled JOIN, and every table it reads recorded as a reference, including the one belonging to
                  the other app.
        """
        source = self.read(self.make_migrations()[-1])

        self.assertIn('INNER JOIN "example_cake"', source)
        self.assertIn("references=('bakery_baker', 'bakery_recipe', 'example_cake')", source)

    def test_a_view_built_on_another_apps_view_reads_that_view(self):
        """
        Case: Read the migration for a view whose queryset is another app's view's manager.
        Expected: A SELECT against that view's table, recorded as a reference to it rather than to the models behind it.
        """
        source = self.read(self.make_migrations()[-1])

        self.assertIn('FROM "example_cakecounts"', source)
        self.assertIn("references=('example_cakecounts',)", source)

    def test_the_view_migration_waits_for_the_app_whose_view_it_reads(self):
        """
        Case: Read the dependencies of the generated view migration.
        Expected: It names the other app's view migration, not merely that app's last model migration.
        """
        self.make_migrations()
        source = self.read(self.written()[-1])
        views_migration = self.written('example')[-1][:-3]

        self.assertIn('view', views_migration)
        self.assertIn("('example', '{}')".format(views_migration), source)

    def test_a_second_run_detects_nothing(self):
        """
        Case: Run makemigrations for both apps twice.
        Expected: Nothing new in either app.
        """
        first = (self.make_migrations(), self.written('example'))

        self.assertEqual((self.make_migrations(), self.written('example')), first)


class UnmigratedAppCommandTestCase(MigrationWritingMixin, TestCase):
    """
    End-to-end cover for the warning: an app declaring database objects but excluded from migrations must not be
    passed over in silence.
    """

    app_label = 'example'

    def test_makemigrations_warns_for_an_unmigrated_app_with_object_changes(self):
        """
        Case: bakery declares views, but its migrations are disabled, and makemigrations runs for example alone.
        Expected: An UnmigratedAppWarning naming bakery. Django deletes bakery's changes and drops the ordering other
                  apps had onto them, reporting success, so this warning is the only trace.
        """
        modules = dict(self.modules)
        modules['bakery'] = None

        with override_settings(MIGRATION_MODULES=modules):
            with self.assertWarnsRegex(UnmigratedAppWarning, 'bakery'):
                call_command('makemigrations', 'example', verbosity=0)

    def test_makemigrations_does_not_warn_for_an_app_with_a_migrations_package(self):
        """
        Case: The same run with every app carrying a migrations package, empty as a fresh app's would be.
        Expected: No warning; every app's changes are written.
        """
        with override_settings(MIGRATION_MODULES=self.modules):
            with warnings.catch_warnings():
                warnings.simplefilter('error', UnmigratedAppWarning)
                call_command('makemigrations', 'example', verbosity=0)


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
