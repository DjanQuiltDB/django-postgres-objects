from django.db import DatabaseError, connection, models
from django.db.migrations.state import ModelState, ProjectState
from django.db.models import F
from django.db.models.functions import Upper
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from postgres_objects import Function, GeneratedField
from postgres_objects.autodetector.recalculation import get_recalculations
from postgres_objects.base import DeclarativeObject
from postgres_objects.operations import AddFunction, AlterFunction, RecalculateGeneratedField, RemoveFunction

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


class RecordingRouter:
    """
    A router that records what it is asked, standing in for one that resolves placement from the model.
    """

    calls = []

    def allow_migrate(self, db, app_label, **hints):
        RecordingRouter.calls.append((app_label, hints))
        return True


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


class DescriptionTestCase(SimpleTestCase):
    def test_each_operation_describes_itself_by_signature(self):
        """
        Case: Ask each operation to describe itself.
        Expected: The signature pointing to the exact overload.
        """
        previous = declare('AllUppercase').definition
        current = declare('AllUppercase', arguments='input TEXT, suffix TEXT').definition

        self.assertEqual(
            AddFunction(current).describe(), 'Create function example_alluppercase(input TEXT, suffix TEXT)'
        )
        self.assertEqual(
            AlterFunction(current, previous).describe(), 'Alter function example_alluppercase(input TEXT, suffix TEXT)'
        )
        self.assertEqual(RemoveFunction(previous).describe(), 'Remove function example_alluppercase(input TEXT)')

    def test_each_operation_names_a_migration_fragment(self):
        """
        Case: Ask each operation for the fragment an auto-generated migration is named after.
        Expected: The action and the object's name (the filename says what the migration does).
        """
        definition = declare('AllUppercase').definition

        self.assertEqual(AddFunction(definition).migration_name_fragment, 'create_function_alluppercase')
        self.assertEqual(AlterFunction(definition, definition).migration_name_fragment, 'alter_function_alluppercase')
        self.assertEqual(RemoveFunction(definition).migration_name_fragment, 'remove_function_alluppercase')

    def test_an_operation_reduces_to_sql_and_is_reversible(self):
        """
        Case: Inspect the flags Django reads off an operation.
        Expected: It reports itself as reducing to SQL and reversible, so sqlmigrate renders it and migrate can unapply
                  it.
        """
        operation = AddFunction(declare('AllUppercase').definition)

        self.assertTrue(operation.reduces_to_sql)
        self.assertTrue(operation.reversible)

    def test_state_forwards_records_nothing(self):
        """
        Case: Apply an operation to the migration project state.
        Expected: The state is untouched, since this kind of object has no representation in it. (What the migrations
                  created is reconstructed from the graph instead.)
        """
        state = ProjectState()
        AddFunction(declare('AllUppercase').definition).state_forwards(APP_LABEL, state)

        self.assertEqual(list(state.models), [])


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

    @override_settings(DATABASE_ROUTERS=['test_operations.RecordingRouter'])
    def test_a_hintless_operation_falls_back_to_the_base_default(self):
        """
        Case: An operation whose migration recorded no hints, applied while DeclarativeObject.router_hints carries a
              placement plugin's default. This indicates a migration written before the plugin was installed.
        Expected: The router receives the base default.
        """
        self.addCleanup(setattr, DeclarativeObject, 'router_hints', DeclarativeObject.router_hints)
        DeclarativeObject.router_hints = {'baking_mode': 'sentinel'}
        RecordingRouter.calls = []

        self.apply(AddFunction(declare('AllUppercase').definition))

        self.assertEqual(RecordingRouter.calls, [(APP_LABEL, {'baking_mode': 'sentinel'})])

    @override_settings(DATABASE_ROUTERS=['test_operations.RecordingRouter'])
    def test_an_operation_with_recorded_hints_keeps_them(self):
        """
        Case: An operation whose migration did record hints, applied while DeclarativeObject.router_hints carries a
              different default.
        Expected: The router receives exactly what the migration recorded; the base default is a fallback only.
        """
        self.addCleanup(setattr, DeclarativeObject, 'router_hints', DeclarativeObject.router_hints)
        DeclarativeObject.router_hints = {'baking_mode': 'sentinel'}
        RecordingRouter.calls = []

        self.apply(AddFunction(declare('AllUppercase').definition, hints={'target': 'here'}))

        self.assertEqual(RecordingRouter.calls, [(APP_LABEL, {'target': 'here'})])

    def test_a_hints_change_pair_leaves_the_function_in_place(self):
        """
        Case: The adjacent drop-and-recreate pair a placement change plans, applied on a connection that allows both
              hint sets, standing in for a project with no routers.
        Expected: The function exists afterwards. The superseding shape would have dropped after the model migrations
                  what its leading create had just written.
        """
        declaration = declare('AllUppercase')
        self.apply(AddFunction(declaration.definition))

        self.apply(RemoveFunction(declaration.definition))
        self.apply(AddFunction(declaration.definition, hints={'target': 'ovens'}))

        self.assertEqual(self.function_exists('example_alluppercase'), 1)

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


class RecalculateDescriptionTestCase(SimpleTestCase):
    def test_it_describes_the_field_and_model(self):
        """
        Case: Ask the operation to describe itself.
        Expected: The field and model.
        """
        operation = RecalculateGeneratedField('GenModel', 'name_uppercased')

        self.assertEqual(operation.describe(), 'Recalculate generated field name_uppercased on GenModel')
        self.assertEqual(operation.migration_name_fragment, 'recalculate_genmodel_name_uppercased')

    def test_it_deconstructs_to_what_it_was_built_from(self):
        """
        Case: Deconstruct the operation the way the migration writer would.
        Expected: The constructor arguments come back, with the hints only when there are any.
        """
        bare = RecalculateGeneratedField('GenModel', 'name_uppercased').deconstruct()
        hinted = RecalculateGeneratedField('GenModel', 'name_uppercased', hints={'target': 'here'}).deconstruct()

        self.assertEqual(bare, ('RecalculateGeneratedField', [], {'model_name': 'GenModel', 'name': 'name_uppercased'}))
        self.assertEqual(
            hinted,
            (
                'RecalculateGeneratedField',
                [],
                {'model_name': 'GenModel', 'name': 'name_uppercased', 'hints': {'target': 'here'}},
            ),
        )

    def test_state_forwards_records_nothing(self):
        """
        Case: Apply the operation to the migration project state.
        Expected: The state is untouched.
        """
        state = ProjectState()
        RecalculateGeneratedField('GenModel', 'name_uppercased').state_forwards(APP_LABEL, state)

        self.assertEqual(list(state.models), [])


class RecalculateTestCase(OperationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self._drop_table)

    def _drop_table(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS gen_dep_table CASCADE')

    def declare_all_uppercase(self, body):
        return declare('AllUppercase', volatility='IMMUTABLE', strict=True, output_field=models.TextField(), body=body)

    def make_state(self, declaration, db_persist=True):
        """
        The project state the migration executor would hand the operation, holding the model whose column is rewritten.
        """
        expression = declaration(F('name')) if declaration else Upper(F('name'))

        state = ProjectState()
        state.add_model(
            ModelState(
                APP_LABEL,
                'GenModel',
                [
                    ('id', models.AutoField(primary_key=True)),
                    ('name', models.TextField()),
                    (
                        'name_uppercased',
                        GeneratedField(expression=expression, output_field=models.TextField(), db_persist=db_persist),
                    ),
                ],
                options={'db_table': 'gen_dep_table'},
            )
        )
        return state

    def create_table_and_row(self):
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE gen_dep_table (
                    id serial PRIMARY KEY,
                    name text,
                    name_uppercased text GENERATED ALWAYS AS (example_alluppercase(name)) STORED
                )
            """)
            cursor.execute("INSERT INTO gen_dep_table (name) VALUES ('cake')")

    def stored_value(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT name_uppercased FROM gen_dep_table')
            return cursor.fetchone()[0]

    def make_stale(self):
        """
        Create the function, a table with a row computed by it, and then alter the body, leaving the stored value stale.
        Returns the state holding the model.
        """
        old = self.declare_all_uppercase('BEGIN RETURN UPPER(input); END;')
        new = self.declare_all_uppercase('BEGIN RETURN LOWER(input); END;')

        self.apply(AddFunction(old.definition))
        self.create_table_and_row()
        self.apply(AlterFunction(new.definition, old.definition))

        return self.make_state(new)

    def recalculate(self, state, operation=None, backwards=False):
        operation = operation or RecalculateGeneratedField('GenModel', 'name_uppercased')
        with connection.schema_editor() as schema_editor:
            if backwards:
                operation.database_backwards(APP_LABEL, schema_editor, state, state)
            else:
                operation.database_forwards(APP_LABEL, schema_editor, state, state)

    def test_stale_values_are_recalculated(self):
        """
        Case: A stored generated column whose function body was replaced, leaving the stored value computed by the old
              body.
        Expected: The operation rewrites the table. Every row gets a correct value for the new function body.
        """
        state = self.make_stale()
        self.assertEqual(self.stored_value(), 'CAKE')

        self.recalculate(state)

        self.assertEqual(self.stored_value(), 'cake')

    def test_backwards_leaves_the_values_alone(self):
        """
        Case: Unapply the operation.
        Expected: Nothing happens.
        """
        state = self.make_stale()

        self.recalculate(state, backwards=True)

        self.assertEqual(self.stored_value(), 'CAKE')

    def test_a_virtual_column_is_left_alone(self):
        """
        Case: Apply the operation to a virtual generated column.
        Expected: No SQL is issued (regeneration is not applicable).
        """
        state = self.make_state(None, db_persist=False)

        with connection.schema_editor(collect_sql=True) as schema_editor:
            RecalculateGeneratedField('GenModel', 'name_uppercased').database_forwards(
                APP_LABEL, schema_editor, state, state
            )

        self.assertEqual(schema_editor.collected_sql, [])

    def test_a_vetoed_recalculation_does_nothing(self):
        """
        Case: A router that refuses every migration on this connection.
        Expected: The stale value stays.
        """
        state = self.make_stale()

        with override_settings(DATABASE_ROUTERS=['test_operations.RefusingRouter']):
            self.recalculate(state)

        self.assertEqual(self.stored_value(), 'CAKE')

    def test_the_router_is_asked_about_the_model(self):
        """
        Case: A router in the way, standing in for a project that resolves placement from the model itself.
        Expected: allow_migrate receives the model name alongside any explicit hints, so a router can answer from
                  either.
        """
        state = self.make_stale()
        RecordingRouter.calls = []

        with override_settings(DATABASE_ROUTERS=['test_operations.RecordingRouter']):
            self.recalculate(state, RecalculateGeneratedField('GenModel', 'name_uppercased', hints={'target': 'here'}))

        self.assertEqual(RecordingRouter.calls, [(APP_LABEL, {'model_name': 'genmodel', 'target': 'here'})])


class PlainGeneratedFieldTestCase(OperationTestCase):
    """
    Django's own GeneratedField over a declared function. The declaration is an expression like any other, so the column
    computes with it, but nothing recalculates the stored value when the function changes: that is what the package's
    own field adds, and a plain one is left to Django.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._drop_table)

    def _drop_table(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS plain_gen_table CASCADE')

    def declare_all_uppercase(self, body):
        return declare('AllUppercase', volatility='IMMUTABLE', strict=True, output_field=models.TextField(), body=body)

    def make_state(self, declaration):
        state = ProjectState()
        state.add_model(
            ModelState(
                APP_LABEL,
                'PlainGenModel',
                [
                    ('id', models.AutoField(primary_key=True)),
                    ('name', models.TextField()),
                    (
                        'name_uppercased',
                        models.GeneratedField(
                            expression=declaration(F('name')), output_field=models.TextField(), db_persist=True
                        ),
                    ),
                ],
                options={'db_table': 'plain_gen_table'},
            )
        )
        return state

    def create_table(self):
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE plain_gen_table (
                    id serial PRIMARY KEY,
                    name text,
                    name_uppercased text GENERATED ALWAYS AS (example_alluppercase(name)) STORED
                )
            """)

    def insert(self, name):
        with connection.cursor() as cursor:
            cursor.execute('INSERT INTO plain_gen_table (name) VALUES (%s)', [name])

    def stored_values(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT name, name_uppercased FROM plain_gen_table ORDER BY id')
            return dict(cursor.fetchall())

    def test_the_column_computes_with_the_declared_function(self):
        """
        Case: A column declared with Django's GeneratedField, calling a declaration for its expression.
        Expected: The column is created and computed by that function, exactly as the package's own field would be.
        """
        self.apply(AddFunction(self.declare_all_uppercase('BEGIN RETURN UPPER(input); END;').definition))
        self.create_table()
        self.insert('cake')

        self.assertEqual(self.stored_values(), {'cake': 'CAKE'})

    def test_a_changed_body_plans_no_recalculation(self):
        """
        Case: The function body is replaced under a column declared with Django's field rather than the package's.
        Expected: Nothing is planned for the column, so the migration carries the function change and nothing else.
        """
        old = self.declare_all_uppercase('BEGIN RETURN UPPER(input); END;')
        new = self.declare_all_uppercase('BEGIN RETURN LOWER(input); END;')

        recalculations = get_recalculations({new.resolved_db_name}, self.make_state(old), self.make_state(new))

        self.assertEqual(recalculations, {})

    def test_written_rows_keep_the_old_value_while_new_ones_do_not(self):
        """
        Case: The body is altered after a row was written, with no recalculation following it.
        Expected: The existing row keeps what the old body produced, while a row inserted afterwards is computed by the
                  new one. The function is live; only the stored value is stale.
        """
        old = self.declare_all_uppercase('BEGIN RETURN UPPER(input); END;')
        new = self.declare_all_uppercase('BEGIN RETURN LOWER(input); END;')

        self.apply(AddFunction(old.definition))
        self.create_table()
        self.insert('cake')
        self.apply(AlterFunction(new.definition, old.definition))
        self.insert('tart')

        self.assertEqual(self.stored_values(), {'cake': 'CAKE', 'tart': 'tart'})
