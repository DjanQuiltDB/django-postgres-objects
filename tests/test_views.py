from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase
from example.models import Cake

from postgres_objects import Change, MaterializedView, View, ViewDefinition
from postgres_objects.operations import AddView, RefreshMaterializedView, RemoveView

APP_LABEL = 'example'
SOURCE_TABLE = 'view_source_table'


def declare(class_name, base=View, **attrs):
    namespace = {'app_label': APP_LABEL, 'sql': 'SELECT id, name FROM example_cake'}
    namespace.update(attrs)

    return type(base)(class_name, (base,), namespace)


class ViewSqlTestCase(SimpleTestCase):
    def test_a_view_is_created_replaceably_and_unqualified(self):
        """
        Case: The CREATE for a plain view.
        Expected: CREATE OR REPLACE, and the view named without a schema so the search path decides where it lands.
        """
        definition = declare('Uppercased').definition

        self.assertEqual(
            definition.create_sql(), 'CREATE OR REPLACE VIEW example_uppercased AS SELECT id, name FROM example_cake;'
        )

    def test_a_trailing_semicolon_in_the_declaration_is_not_doubled(self):
        """
        Case: A declaration whose sql ends in a semicolon.
        Expected: One semicolon in the statement, so writing it either way works.
        """
        definition = declare('Uppercased', sql='SELECT id FROM example_cake;').definition

        self.assertEqual(
            definition.create_sql(), 'CREATE OR REPLACE VIEW example_uppercased AS SELECT id FROM example_cake;'
        )

    def test_options_become_a_with_clause(self):
        """
        Case: A view declaring reloptions.
        Expected: They are rendered into the CREATE, sorted so the statement does not change between runs.
        """
        definition = declare('Uppercased', options={'security_invoker': 'true', 'check_option': 'cascaded'}).definition

        self.assertIn('WITH (check_option=cascaded, security_invoker=true)', definition.create_sql())

    def test_a_view_is_dropped_without_cascade(self):
        """
        Case: The DROP for a plain view.
        Expected: Schema-qualified and without CASCADE, so an ordering mistake fails loudly rather than taking a
                  dependent object with it.
        """
        definition = declare('Uppercased').definition

        self.assertEqual(definition.drop_sql('public'), 'DROP VIEW IF EXISTS "public".example_uppercased;')

    def test_a_materialized_view_has_no_replacing_form(self):
        """
        Case: The CREATE for a materialized view.
        Expected: A plain CREATE, since Postgres has no CREATE OR REPLACE MATERIALIZED VIEW.
        """
        definition = declare('Totals', base=MaterializedView).definition

        self.assertTrue(definition.create_sql().startswith('CREATE MATERIALIZED VIEW example_totals AS '))

    def test_an_unpopulated_materialized_view_says_so(self):
        """
        Case: A materialized view declared with with_data False.
        Expected: WITH NO DATA, so creating it stays cheap and it is filled by a later refresh.
        """
        definition = declare('Totals', base=MaterializedView, with_data=False).definition

        self.assertTrue(definition.create_sql().endswith(' WITH NO DATA;'))

    def test_declared_indexes_follow_the_create(self):
        """
        Case: A materialized view declaring a unique index and a further one.
        Expected: One statement per index after the CREATE, the unique one first, named after the view and columns so
                  the names are stable between runs.
        """
        definition = declare('Totals', base=MaterializedView, unique_index=('name',), indexes=[('id',)]).definition

        statements = definition.create_statements()

        self.assertEqual(len(statements), 3)
        self.assertEqual(
            statements[1], 'CREATE UNIQUE INDEX IF NOT EXISTS "example_totals_name_key" ON example_totals ("name");'
        )
        self.assertEqual(statements[2], 'CREATE INDEX IF NOT EXISTS "example_totals_id_idx" ON example_totals ("id");')

    def test_a_plain_view_is_one_statement(self):
        """
        Case: A plain view, which declares no indexes.
        Expected: Exactly the CREATE, so nothing changes for the simple case.
        """
        definition = declare('Uppercased').definition

        self.assertEqual(definition.create_statements(), (definition.create_sql(),))


class ViewReferenceTestCase(SimpleTestCase):
    def test_a_raw_sql_view_reads_nothing_it_has_not_been_told_about(self):
        """
        Case: A raw-sql declaration with no depends_on.
        Expected: No references.
        """
        self.assertEqual(declare('Uppercased').definition.references, ())

    def test_depends_on_names_a_declaration_a_model_or_a_table(self):
        """
        Case: The three things depends_on accepts.
        Expected: All three normalized to the identifier the SQL names the relation by.
        """
        source = declare('Source')
        declaration = declare('Uppercased', depends_on=[source, Cake, 'some_table'])

        self.assertEqual(declaration.references, ('example_cake', 'example_source', 'some_table'))

    def test_depends_on_refuses_what_names_no_relation(self):
        """
        Case: depends_on given a value that is not a class, and a class that is neither a model nor a declaration.
        Expected: ImproperlyConfigured for both.
        """
        for reference in (object(), dict):
            with self.subTest(reference=reference):
                with self.assertRaisesMessage(ImproperlyConfigured, 'depends_on takes View declarations'):
                    declare('Uppercased', depends_on=[reference]).definition

    def test_depends_on_refuses_an_abstract_declaration(self):
        """
        Case: depends_on naming an abstract declaration, which is created as nothing.
        Expected: Refused, since there is no relation to wait for.
        """
        base = declare('Base', abstract=True)

        with self.assertRaisesMessage(ImproperlyConfigured, 'Base is abstract'):
            declare('Uppercased', depends_on=[base]).definition

    def test_a_view_does_not_read_itself(self):
        """
        Case: A declaration naming its own table, which a recursive-looking body invites.
        Expected: Dropped, because an operation that had to run after itself could never be ordered.
        """
        self.assertEqual(declare('Uppercased', depends_on=['example_uppercased']).references, ())

    def test_the_references_are_carried_into_the_migration(self):
        """
        Case: The deconstruction a migration is written from.
        Expected: references among the fields.
        """
        _, _, kwargs = declare('Uppercased', depends_on=[Cake]).definition.deconstruct()

        self.assertEqual(kwargs['references'], ('example_cake',))

    def test_a_definition_written_by_hand_needs_no_references(self):
        """
        Case: A ViewDefinition built without naming references, as the documentation shows.
        Expected: Accepted, reading as a view whose references are simply not recorded.
        """
        definition = ViewDefinition(name='uppercased', db_name='example_uppercased', sql='SELECT 1', options={})

        self.assertEqual(definition.references, ())


class ViewPlanChangeTestCase(SimpleTestCase):
    def test_an_identical_declaration_is_unchanged(self):
        """
        Case: The same view declared twice.
        Expected: UNCHANGED, which is what keeps makemigrations --check quiet.
        """
        first = declare('Uppercased').definition
        second = declare('Uppercased').definition

        self.assertIs(second.plan_change_from(first), Change.UNCHANGED)

    def test_any_change_supersedes_rather_than_altering(self):
        """
        Case: An edited SELECT.
        Expected: SUPERSEDE, so the old view is dropped before the model migrations and the new one created after them,
                  rather than a CREATE OR REPLACE that would run too late to free a column being dropped.
        """
        previous = declare('Uppercased').definition
        current = declare('Uppercased', sql='SELECT id FROM example_cake').definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_becoming_materialized_is_a_change_of_the_same_object(self):
        """
        Case: A plain view redeclared as a materialized one.
        Expected: SUPERSEDE rather than an unrelated object appearing, since both share the identifier and the old one
                  has to go first.
        """
        previous = declare('Uppercased').definition
        current = declare('Uppercased', base=MaterializedView).definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_a_view_is_created_after_the_model_migrations(self):
        """
        Case: The placement of a view against that of a function.
        Expected: A view does not precede the model migrations, which is what inverts the ordering of its operations.
        """
        self.assertFalse(declare('Uppercased').definition.precedes_models)


class ViewOperationTestCase(TransactionTestCase):
    """
    Operations create real views, which survive a rolled back transaction the way table data does not, so these run as
    TransactionTestCase and clean up after themselves.

    They read from a table created here rather than from the example model's, whose generated column needs a function
    that the test database is not built with.
    """

    available_apps = ['postgres_objects', 'example']

    def declare_view(self, class_name, base=View, **attrs):
        attrs.setdefault('sql', 'SELECT id, name FROM {}'.format(SOURCE_TABLE))
        return declare(class_name, base=base, **attrs)

    def setUp(self):
        super().setUp()
        self._created = []
        self.addCleanup(self._drop_created_views)
        self.addCleanup(self._drop_source_table)

        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE {} (id serial PRIMARY KEY, name text)'.format(SOURCE_TABLE))

    def _drop_source_table(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS {} CASCADE'.format(SOURCE_TABLE))

    def _drop_created_views(self):
        with connection.cursor() as cursor:
            for definition in self._created:
                statement = 'DROP MATERIALIZED VIEW' if definition.materialized else 'DROP VIEW'
                cursor.execute('{} IF EXISTS {} CASCADE;'.format(statement, definition.db_name))

    def apply(self, operation, backwards=False):
        self._created.append(operation.definition)
        with connection.schema_editor() as schema_editor:
            if backwards:
                operation.database_backwards(APP_LABEL, schema_editor, None, None)
            else:
                operation.database_forwards(APP_LABEL, schema_editor, None, None)

    def relkind(self, db_name):
        with connection.cursor() as cursor:
            cursor.execute('SELECT relkind FROM pg_catalog.pg_class WHERE relname = %s', [db_name])
            row = cursor.fetchone()
            return row and row[0]

    def test_adding_a_view_creates_it_and_reversing_drops_it(self):
        """
        Case: Apply AddView and then reverse it.
        Expected: The view exists and is gone again, so an unapplied migration leaves nothing behind.
        """
        definition = self.declare_view('Uppercased').definition

        self.apply(AddView(definition))
        self.assertEqual(self.relkind('example_uppercased'), 'v')

        self.apply(AddView(definition), backwards=True)
        self.assertIsNone(self.relkind('example_uppercased'))

    def test_a_view_selects_from_its_table(self):
        """
        Case: Query a created view after inserting a row into the table it reads.
        Expected: The row comes back, so the view is wired to the real table rather than merely existing.
        """
        definition = self.declare_view('Uppercased').definition
        self.apply(AddView(definition))

        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO {} (name) VALUES ('lemon')".format(SOURCE_TABLE))
            cursor.execute('SELECT name FROM example_uppercased')

            self.assertEqual(cursor.fetchone()[0], 'lemon')

    def test_removing_a_view_drops_it_and_reversing_brings_it_back(self):
        """
        Case: Apply RemoveView to an existing view, then reverse it.
        Expected: Gone, then recreated, so a removal migration is reversible.
        """
        definition = self.declare_view('Uppercased').definition
        self.apply(AddView(definition))

        self.apply(RemoveView(definition))
        self.assertIsNone(self.relkind('example_uppercased'))

        self.apply(RemoveView(definition), backwards=True)
        self.assertEqual(self.relkind('example_uppercased'), 'v')

    def test_a_materialized_view_is_created_with_its_indexes(self):
        """
        Case: Apply AddView for a materialized view declaring a unique index.
        Expected: A materialized view carrying that index, which is what lets it be refreshed concurrently.
        """
        definition = self.declare_view(
            'Totals',
            base=MaterializedView,
            sql='SELECT name FROM {} GROUP BY name'.format(SOURCE_TABLE),
            unique_index=('name',),
        ).definition

        self.apply(AddView(definition))

        self.assertEqual(self.relkind('example_totals'), 'm')
        with connection.cursor() as cursor:
            cursor.execute('SELECT indexname FROM pg_indexes WHERE tablename = %s', ['example_totals'])
            self.assertEqual([row[0] for row in cursor.fetchall()], ['example_totals_name_key'])

    def test_a_mixed_case_index_column_is_quoted(self):
        """
        Case: Apply AddView for a materialized view whose unique index covers a column the SELECT aliases with a
              capital letter in it, the way a compiled queryset writes an annotated alias.
        Expected: The index is created over that very column. Unquoted, CREATE INDEX would fold the name to lowercase
                  and fail against the view at migrate time.
        """
        definition = self.declare_view(
            'Totals',
            base=MaterializedView,
            sql='SELECT name AS "mixedName" FROM {} GROUP BY name'.format(SOURCE_TABLE),
            unique_index=('mixedName',),
        ).definition

        self.apply(AddView(definition))

        with connection.cursor() as cursor:
            cursor.execute('SELECT indexdef FROM pg_indexes WHERE tablename = %s', ['example_totals'])
            rows = cursor.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertIn('"mixedName"', rows[0][0])

    def test_an_unpopulated_materialized_view_is_filled_by_a_refresh(self):
        """
        Case: Create a materialized view WITH NO DATA, then refresh it.
        Expected: Unreadable until refreshed, and populated afterwards, which is the point of deferring the fill.
        """
        definition = self.declare_view(
            'Totals', base=MaterializedView, sql='SELECT name FROM {}'.format(SOURCE_TABLE), with_data=False
        ).definition

        self.apply(AddView(definition))
        self.assertFalse(self.is_populated('example_totals'))

        self.apply(RefreshMaterializedView(definition))
        self.assertTrue(self.is_populated('example_totals'))

    def test_a_concurrent_refresh_keeps_the_view_readable(self):
        """
        Case: Refresh a populated materialized view concurrently.
        Expected: It succeeds, since the declared unique index is what Postgres requires for it.
        """
        definition = self.declare_view(
            'Totals',
            base=MaterializedView,
            sql='SELECT name FROM {} GROUP BY name'.format(SOURCE_TABLE),
            unique_index=('name',),
        ).definition
        self.apply(AddView(definition))

        self.apply(RefreshMaterializedView(definition, concurrently=True))

        self.assertTrue(self.is_populated('example_totals'))

    def is_populated(self, db_name):
        with connection.cursor() as cursor:
            cursor.execute('SELECT relispopulated FROM pg_catalog.pg_class WHERE relname = %s', [db_name])
            return cursor.fetchone()[0]


class RefreshMaterializedViewTestCase(SimpleTestCase):
    def test_refreshing_a_plain_view_is_refused(self):
        """
        Case: Build a refresh operation for a view that stores nothing.
        Expected: ValueError when the operation is written, rather than a database error at migrate time.
        """
        with self.assertRaises(ValueError) as caught:
            RefreshMaterializedView(declare('Uppercased').definition)

        self.assertIn('not a materialized view', str(caught.exception))

    def test_a_concurrent_refresh_without_a_unique_index_is_refused(self):
        """
        Case: Ask for a concurrent refresh of a view declaring no unique index.
        Expected: ValueError naming what is missing, since Postgres would otherwise refuse at migrate time.
        """
        definition = declare('Totals', base=MaterializedView).definition

        with self.assertRaises(ValueError) as caught:
            RefreshMaterializedView(definition, concurrently=True)

        self.assertIn('unique_index', str(caught.exception))

    def test_a_refresh_changes_nothing_about_what_exists(self):
        """
        Case: The flags the autodetector folds the migration graph with.
        Expected: A refresh neither creates nor removes, so folding it does not make the view look newly created.
        """
        operation = RefreshMaterializedView(declare('Totals', base=MaterializedView).definition)

        self.assertFalse(operation.creates)
        self.assertFalse(operation.removes)

    def test_reversing_a_refresh_does_nothing(self):
        """
        Case: Reverse a refresh operation.
        Expected: It completes without touching the database, since a refresh has no inverse and a rollback must not
                  fail on that account.
        """
        operation = RefreshMaterializedView(declare('Totals', base=MaterializedView).definition)

        operation.database_backwards(APP_LABEL, None, None, None)


class ViewDescriptionTestCase(SimpleTestCase):
    def test_operations_describe_themselves_by_kind(self):
        """
        Case: What makemigrations prints for view operations.
        Expected: The noun matches the kind of object, so a materialized view is not described as a plain one.
        """
        view = declare('Uppercased').definition
        materialized = declare('Totals', base=MaterializedView).definition

        self.assertEqual(AddView(view).describe(), 'Create view example_uppercased')
        self.assertEqual(RemoveView(view).describe(), 'Remove view example_uppercased')
        self.assertEqual(AddView(materialized).describe(), 'Create materialized view example_totals')

    def test_the_migration_name_fragment_names_the_view(self):
        """
        Case: The fragment an auto-generated migration is named after.
        Expected: Verb, kind and name, so a generated file reads as what it does.
        """
        self.assertEqual(AddView(declare('Uppercased').definition).migration_name_fragment, 'create_view_uppercased')
