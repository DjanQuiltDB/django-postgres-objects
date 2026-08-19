from django.db import DatabaseError, connection
from django.test import TransactionTestCase, override_settings

from postgres_objects import Function
from postgres_objects.operations import AddFunction, AlterFunction, RemoveFunction

APP_LABEL = 'example'


def declare(class_name, **attrs):
    namespace = {
        'app_label': APP_LABEL,
        'arguments': 'input TEXT',
        'returns': 'TEXT',
        'body': """
            BEGIN
                RETURN input;
            END;
        """,
    }
    namespace.update(attrs)

    return type(Function)(class_name, (Function,), namespace)


class RefusingRouter:
    """
    A router that vetoes every migration, standing in for a project that routes objects somewhere else.
    """

    def allow_migrate(self, db, app_label, **hints):
        return False


class HintReadingRouter:
    """
    A router that only allows an operation carrying a matching hint.
    """

    def allow_migrate(self, db, app_label, **hints):
        return hints.get('target') == 'here'


class OperationTestCase(TransactionTestCase):
    """
    Operations create real functions, and a function survives a rolled back transaction the way table data does not, so
    these run as TransactionTestCase and clean up after themselves.
    """

    available_apps = ['postgres_objects', 'example']

    def setUp(self):
        super().setUp()
        self.addCleanup(self._drop_created_functions)
        self._created = []

    def _drop_created_functions(self):
        with connection.cursor() as cursor:
            for definition in self._created:
                cursor.execute('DROP FUNCTION IF EXISTS {} CASCADE;'.format(definition.drop_signature))

    def apply(self, operation, backwards=False):
        self._created.append(operation.definition)
        with connection.schema_editor() as schema_editor:
            if backwards:
                operation.database_backwards(APP_LABEL, schema_editor, None, None)
            else:
                operation.database_forwards(APP_LABEL, schema_editor, None, None)

    def function_exists(self, db_name, schema_name='public'):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pg_catalog.pg_proc proc
                JOIN pg_catalog.pg_namespace nsp ON proc.pronamespace = nsp.oid
                WHERE proc.proname = %s AND nsp.nspname = %s
                """,
                [db_name, schema_name],
            )
            return cursor.fetchone()[0]

    def call(self, db_name, argument):
        with connection.cursor() as cursor:
            cursor.execute('SELECT {}(%s)'.format(db_name), [argument])
            return cursor.fetchone()[0]


class AddFunctionTestCase(OperationTestCase):
    def test_it_creates_the_function(self):
        """
        Case: Apply AddFunction forwards.
        Expected: The function exists and is callable with the declared body.
        """
        declaration = declare('AllUppercase', body='BEGIN RETURN UPPER(input); END;')

        self.apply(AddFunction(declaration.definition))

        self.assertEqual(self.function_exists('example_alluppercase'), 1)
        self.assertEqual(self.call('example_alluppercase', 'cake'), 'CAKE')

    def test_it_is_reversible(self):
        """
        Case: Apply AddFunction and then reverse it.
        Expected: The function is gone, so an unapplied migration leaves nothing behind.
        """
        declaration = declare('AllUppercase')
        operation = AddFunction(declaration.definition)
        self.apply(operation)

        self.apply(operation, backwards=True)

        self.assertEqual(self.function_exists('example_alluppercase'), 0)

    def test_a_function_with_an_argument_default_can_be_removed(self):
        """
        Case: Reverse AddFunction for a function with a parameter default.
        Expected: The reversal succeeds, because DROP FUNCTION names the types without the default clause.
        """
        declaration = declare(
            'WithDefault',
            arguments="input TEXT, suffix TEXT DEFAULT '!'",
            body='BEGIN RETURN input || suffix; END;',
        )
        operation = AddFunction(declaration.definition)
        self.apply(operation)

        self.apply(operation, backwards=True)

        self.assertEqual(self.function_exists('example_withdefault'), 0)

    def test_a_function_with_an_out_parameter_can_be_removed(self):
        """
        Case: Reverse AddFunction for a function with an OUT parameter.
        Expected: The reversal succeeds, because the parameter mode is kept in the drop signature.
        """
        declaration = declare(
            'WithOut', arguments='input INT, OUT result INT', returns='INT', body='BEGIN result := input * 2; END;'
        )
        operation = AddFunction(declaration.definition)
        self.apply(operation)

        self.apply(operation, backwards=True)

        self.assertEqual(self.function_exists('example_without'), 0)

    def test_a_percent_in_the_body_is_not_read_as_a_placeholder(self):
        """
        Case: A body containing a literal %, which the DB-API would otherwise treat as a parameter placeholder.
        Expected: The function is created with the body verbatim.
        """
        declaration = declare(
            'IsEven', arguments='input INT', returns='BOOLEAN', body='BEGIN RETURN input % 2 = 0; END;'
        )

        self.apply(AddFunction(declaration.definition))

        with connection.cursor() as cursor:
            cursor.execute('SELECT example_iseven(4)')
            self.assertTrue(cursor.fetchone()[0])

    def test_a_percent_in_a_comment_is_not_read_as_a_placeholder(self):
        """
        Case: A body whose comment contains a literal %.
        Expected: The function is created, so a percentage in prose does not have to be escaped.
        """
        declaration = declare(
            'Doubled',
            arguments='input INT',
            returns='INT',
            body='BEGIN\n-- increase the input by 100% of itself\nRETURN input * 2;\nEND;',
        )

        self.apply(AddFunction(declaration.definition))

        with connection.cursor() as cursor:
            cursor.execute('SELECT example_doubled(21)')
            self.assertEqual(cursor.fetchone()[0], 42)


class AlterFunctionTestCase(OperationTestCase):
    def test_it_replaces_the_body(self):
        """
        Case: Alter a function whose signature and return type are unchanged.
        Expected: CREATE OR REPLACE swaps the body in place, with no drop in between.
        """
        previous = declare('AllUppercase', body='BEGIN RETURN input; END;').definition
        current = declare('AllUppercase', body='BEGIN RETURN UPPER(input); END;').definition
        self.apply(AddFunction(previous))

        self.apply(AlterFunction(current, previous))

        self.assertEqual(self.call('example_alluppercase', 'cake'), 'CAKE')

    def test_it_reverses_to_the_previous_body(self):
        """
        Case: Reverse an AlterFunction.
        Expected: The body the function had before is restored, which is why the operation carries the previous
                  definition at all.
        """
        previous = declare('AllUppercase', body='BEGIN RETURN input; END;').definition
        current = declare('AllUppercase', body='BEGIN RETURN UPPER(input); END;').definition
        self.apply(AddFunction(previous))
        operation = AlterFunction(current, previous)
        self.apply(operation)

        self.apply(operation, backwards=True)

        self.assertEqual(self.call('example_alluppercase', 'cake'), 'cake')


class RemoveFunctionTestCase(OperationTestCase):
    def test_it_drops_the_function(self):
        """
        Case: Apply RemoveFunction forwards.
        Expected: The function is gone.
        """
        definition = declare('AllUppercase').definition
        self.apply(AddFunction(definition))

        self.apply(RemoveFunction(definition))

        self.assertEqual(self.function_exists('example_alluppercase'), 0)

    def test_it_reverses_by_creating_it_again(self):
        """
        Case: Reverse a RemoveFunction.
        Expected: The function is recreated, so unapplying the migration restores what it removed.
        """
        definition = declare('AllUppercase').definition
        self.apply(AddFunction(definition))
        operation = RemoveFunction(definition)
        self.apply(operation)

        self.apply(operation, backwards=True)

        self.assertEqual(self.function_exists('example_alluppercase'), 1)


class TargetSchemaTestCase(OperationTestCase):
    def setUp(self):
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA IF NOT EXISTS other_schema')
        self.addCleanup(self._drop_schema)

    def _drop_schema(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP SCHEMA IF EXISTS other_schema CASCADE')

    def test_the_target_schema_is_the_one_the_create_lands_in(self):
        """
        Case: A connection whose search path starts with a schema other than public.
        Expected: The create lands in that schema and target_schema names the same one, so the two always agree.
        """
        definition = declare('AllUppercase').definition

        with connection.cursor() as cursor:
            cursor.execute('SET search_path = other_schema, public')
            with connection.schema_editor() as schema_editor:
                AddFunction(definition).database_forwards(APP_LABEL, schema_editor, None, None)

            self.assertEqual(self.function_exists('example_alluppercase', schema_name='other_schema'), 1)

            with connection.schema_editor() as schema_editor:
                self.assertEqual(RemoveFunction(definition).target_schema(schema_editor), 'other_schema')

            cursor.execute('SET search_path = public')

    def test_a_drop_does_not_reach_past_its_own_schema(self):
        """
        Case: The same function exists in public and in a schema earlier on the search path, and only the earlier one
              was created here.
        Expected: The drop removes the one in its own schema and leaves the public copy alone.
        """
        definition = declare('AllUppercase').definition
        self.apply(AddFunction(definition))

        with connection.cursor() as cursor:
            cursor.execute('SET search_path = other_schema, public')
            with connection.schema_editor() as schema_editor:
                AddFunction(definition).database_forwards(APP_LABEL, schema_editor, None, None)
            with connection.schema_editor() as schema_editor:
                RemoveFunction(definition).database_forwards(APP_LABEL, schema_editor, None, None)
            cursor.execute('SET search_path = public')

        self.assertEqual(self.function_exists('example_alluppercase', schema_name='other_schema'), 0)
        self.assertEqual(self.function_exists('example_alluppercase', schema_name='public'), 1)


class RoutingTestCase(OperationTestCase):
    @override_settings(DATABASE_ROUTERS=['test_operations.RefusingRouter'])
    def test_a_vetoed_operation_does_nothing(self):
        """
        Case: A router that refuses every migration on this connection.
        Expected: No function is created, so an object can be kept off a database entirely.
        """
        declaration = declare('AllUppercase')

        self.apply(AddFunction(declaration.definition))

        self.assertEqual(self.function_exists('example_alluppercase'), 0)

    @override_settings(DATABASE_ROUTERS=['test_operations.HintReadingRouter'])
    def test_the_hints_are_handed_to_the_router(self):
        """
        Case: A router that only allows operations carrying a matching hint.
        Expected: The operation declaring the hint runs and the one without it is skipped.
        """
        self.apply(AddFunction(declare('Skipped').definition))
        self.apply(AddFunction(declare('Allowed').definition, hints={'target': 'here'}))

        self.assertEqual(self.function_exists('example_skipped'), 0)
        self.assertEqual(self.function_exists('example_allowed'), 1)

    def test_no_router_allows_everything(self):
        """
        Case: A project with no routers configured, which is the common case.
        Expected: An operation with no hints runs.
        """
        self.apply(AddFunction(declare('AllUppercase').definition))

        self.assertEqual(self.function_exists('example_alluppercase'), 1)


class GeneratedColumnDependencyTestCase(OperationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self._drop_table)

    def _drop_table(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS gen_dep_table CASCADE')

    def test_a_dependent_generated_column_blocks_the_drop(self):
        """
        Case: Drop a function while a stored generated column still calls it.
        Expected: Postgres refuses, which is exactly why removals are ordered after the model migrations rather than
                  before them.
        """
        definition = declare(
            'AllUppercase', volatility='IMMUTABLE', strict=True, body='BEGIN RETURN UPPER(input); END;'
        )
        definition = definition.definition
        self.apply(AddFunction(definition))

        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE gen_dep_table (
                    id serial PRIMARY KEY,
                    name text,
                    name_uppercased text GENERATED ALWAYS AS (example_alluppercase(name)) STORED
                )
            """)

        with self.assertRaises(DatabaseError):
            self.apply(RemoveFunction(definition))

    def test_a_signature_change_orders_around_a_dependent_generated_column(self):
        """
        Case: A generated column depends on a function whose signature changes.
        Expected: Creating the new overload first, moving the column onto it, then dropping the old one succeeds.
        """
        old = declare(
            'AllUppercase', volatility='IMMUTABLE', strict=True, body='BEGIN RETURN UPPER(input); END;'
        ).definition
        new = declare(
            'AllUppercase',
            arguments='input TEXT, suffix TEXT',
            volatility='IMMUTABLE',
            strict=True,
            body='BEGIN RETURN UPPER(input) || suffix; END;',
        ).definition

        self.apply(AddFunction(old))
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE gen_dep_table (
                    id serial PRIMARY KEY,
                    name text,
                    name_uppercased text GENERATED ALWAYS AS (example_alluppercase(name)) STORED
                )
            """)

        self.apply(AddFunction(new))
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE gen_dep_table DROP COLUMN name_uppercased')
            cursor.execute("""
                ALTER TABLE gen_dep_table
                ADD COLUMN name_uppercased text GENERATED ALWAYS AS (example_alluppercase(name, '!')) STORED
            """)

        self.apply(RemoveFunction(old))

        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO gen_dep_table (name) VALUES ('cake') RETURNING name_uppercased")
            self.assertEqual(cursor.fetchone()[0], 'CAKE!')
