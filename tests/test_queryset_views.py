import pickle

from bakery.models import Recipe
from django.apps import apps
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldError
from django.db import connection, models
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.db.models import Count, F, Subquery, Value
from django.test import SimpleTestCase, TransactionTestCase
from example.models import BundtOrder, Cake

from postgres_objects import Change, MaterializedView, View, ViewDefinition
from postgres_objects.operations import AddView, RefreshMaterializedView

APP_LABEL = 'example'


def declare(class_name, base=View, **attrs):
    namespace = {'app_label': APP_LABEL}
    namespace.update(attrs)

    return type(base)(class_name, (base,), namespace)


def cake_names():
    return Cake.objects.values('id', 'name')


class PickledCakes(View):
    """
    Declared at module level so that unpickling can find its way back here by import.
    """

    app_label = APP_LABEL

    def queryset():
        return Cake.objects.values('id', 'name')


class ViewBodyTestCase(SimpleTestCase):
    def test_declaring_both_sql_and_a_queryset_is_refused(self):
        """
        Case: A declaration carrying both a raw sql string and a queryset method.
        Expected: TypeError naming both.
        """
        declaration = declare('Uppercased', sql='SELECT id FROM example_cake', queryset=cake_names)

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('both sql and queryset', str(caught.exception))

    def test_declaring_neither_sql_nor_a_queryset_is_refused(self):
        """
        Case: A declaration with no body at all.
        Expected: TypeError saying there is nothing to select from.
        """
        declaration = declare('Uppercased')

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('neither sql nor queryset', str(caught.exception))

    def test_an_abstract_declaration_has_no_definition(self):
        """
        Case: An abstract declaration carrying a queryset.
        Expected: TypeError saying there is nothing to select from.
        """
        declaration = declare('Uppercased', abstract=True, queryset=cake_names)

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('abstract', str(caught.exception))

    def test_raw_sql_resolves_to_itself(self):
        """
        Case: A raw-sql declaration read through resolved_sql.
        Expected: The string exactly as written.
        """
        declaration = declare('Uppercased', sql='SELECT id FROM example_cake')

        self.assertEqual(declaration.resolved_sql, 'SELECT id FROM example_cake')


class CompiledDefinitionTestCase(SimpleTestCase):
    def test_a_queryset_compiles_into_an_ordinary_view_definition(self):
        """
        Case: The definition of a queryset-declared view.
        Expected: A plain ViewDefinition whose sql selects the queryset's columns by name.
        """
        definition = declare('Uppercased', queryset=cake_names).definition

        self.assertIs(type(definition), ViewDefinition)
        self.assertIn('"example_cake"."id"', definition.sql)
        self.assertIn('"example_cake"."name"', definition.sql)

    def test_compiling_twice_detects_no_change(self):
        """
        Case: The same queryset declared twice, as makemigrations --check would see it.
        Expected: Equal definitions and UNCHANGED, so an unedited declaration never writes a migration.
        """
        first = declare('Uppercased', queryset=cake_names).definition
        second = declare('Uppercased', queryset=cake_names).definition

        self.assertEqual(first, second)
        self.assertIs(second.plan_change_from(first), Change.UNCHANGED)

    def test_the_compiled_sql_carries_no_trailing_semicolon(self):
        """
        Case: The compiled SELECT rendered into a CREATE.
        Expected: create_sql() ends with exactly one semicolon.
        """
        definition = declare('Uppercased', queryset=cake_names).definition

        self.assertFalse(definition.sql.endswith(';'))
        self.assertTrue(definition.create_sql().endswith(';'))
        self.assertNotIn(';;', definition.create_sql())

    def test_a_filter_parameter_is_inlined_as_a_literal(self):
        """
        Case: A queryset filtering on a string, which compiles to SQL with a %s placeholder.
        Expected: The literal written into the SQL and no placeholder left, since a view carries no parameters.
        """
        definition = declare(
            'Uppercased', queryset=lambda: Cake.objects.values('id').filter(name__startswith='Choc')
        ).definition

        self.assertIn("'Choc%'", definition.sql)
        self.assertNotIn('%s', definition.sql)

    def test_a_quote_in_a_parameter_is_escaped(self):
        """
        Case: A filter value containing a single quote.
        Expected: Escaped inside the literal rather than terminating it.
        """
        definition = declare('Uppercased', queryset=lambda: Cake.objects.values('id').filter(name="Choc'o")).definition

        self.assertIn("'Choc''o'", definition.sql)

    def test_a_parameterless_pattern_lookup_keeps_its_percent(self):
        """
        Case: A column-to-column __contains, which Django compiles with literal %% and no parameters at all.
        Expected: The doubled percent collapses to the one the database should see.
        """
        definition = declare(
            'Uppercased', queryset=lambda: Cake.objects.values('id').filter(name__contains=F('name_uppercased'))
        ).definition

        self.assertNotIn('%%', definition.sql)
        self.assertIn('%', definition.sql)

    def test_the_table_a_queryset_reads_is_recorded(self):
        """
        Case: A queryset over one model.
        Expected: That model's table is recorded.
        """
        self.assertEqual(declare('Uppercased', queryset=cake_names).references, ('example_cake',))

    def test_every_table_a_join_reads_is_recorded(self):
        """
        Case: A queryset traversing a relation, which compiles to a JOIN.
        Expected: Both tables are recorded.
        """
        declaration = declare('Credits', queryset=lambda: Recipe.objects.values('id', baker_name=F('baker__name')))

        self.assertEqual(declaration.references, ('bakery_baker', 'bakery_recipe'))

    def test_a_table_read_only_by_a_subquery_is_recorded(self):
        """
        Case: A queryset whose only mention of a model is inside a subquery.
        Expected: The subquery model is recorded.
        """
        declaration = declare(
            'Uppercased',
            queryset=lambda: Cake.objects.values('id').filter(id__in=Recipe.objects.values('cake_id')),
        )

        self.assertEqual(declaration.references, ('bakery_recipe', 'example_cake'))

    def test_a_view_reads_the_view_whose_model_its_queryset_is_built_on(self):
        """
        Case: A queryset over another declaration's generated model.
        Expected: A SELECT against that view's table, and the view recorded as what it reads.
        """
        source = declare('Uppercased', queryset=cake_names)
        stacked = declare('Stacked', queryset=lambda: source.objects.values('id'))

        self.assertIn('FROM "example_uppercased"', stacked.definition.sql)
        self.assertEqual(stacked.references, ('example_uppercased',))

    def test_a_combinator_queryset_is_refused(self):
        """
        Case: A union of two querysets, whose columns Django names col1, col2, ...
        Expected: TypeError pointing at raw sql.
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects.values('id').union(Cake.objects.values('id')))

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('sql', str(caught.exception))

    def test_a_manager_instead_of_a_queryset_is_refused(self):
        """
        Case: A queryset method returning the manager itself.
        Expected: TypeError saying to add .all(), rather than an AttributeError from deep inside the compiler.
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects)

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('.all()', str(caught.exception))

    def test_duplicate_column_names_are_refused(self):
        """
        Case: select_related, which selects both tables' id columns bare.
        Expected: TypeError naming the duplicated column.
        """
        declaration = declare('Uppercased', queryset=lambda: Permission.objects.select_related('content_type'))

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn("'id'", str(caught.exception))

    def test_something_other_than_a_queryset_is_refused(self):
        """
        Case: A queryset method returning a list.
        Expected: TypeError saying what was expected, since nothing else carries a compilable query.
        """
        declaration = declare('Uppercased', queryset=lambda: [1, 2, 3])

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('must return a queryset', str(caught.exception))

    def test_a_queryset_selecting_nothing_is_refused(self):
        """
        Case: A queryset Django can prove returns no rows, which compiles to no SQL at all.
        Expected: TypeError saying there is no view to create.
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects.values('id').filter(pk__in=[]))

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('selects nothing', str(caught.exception))

    def test_a_column_without_a_single_name_is_refused(self):
        """
        Case: values('pk') on a model whose primary key is composite, so one select entry expands to two columns.
        Expected: TypeError pointing at values() or annotate(), rather than a view with a guessed column. (Selecting the
                  model bare works, since that expands to the individual columns.)
        """
        from django.apps.registry import Apps

        class Duo(models.Model):
            pk = models.CompositePrimaryKey('first', 'second')
            first = models.IntegerField()
            second = models.IntegerField()

            class Meta:
                apps = Apps(installed_apps=[])
                app_label = APP_LABEL

        declaration = declare('Uppercased', queryset=lambda: Duo.objects.values('pk'))

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn('values()', str(caught.exception))

    def test_an_annotation_of_unknowable_type_is_refused(self):
        """
        Case: An annotation mixing types without declaring an output_field.
        Expected: Django's own FieldError, raised while compiling, whose message already names the fix.
        """
        declaration = declare(
            'Uppercased', queryset=lambda: Cake.objects.values('id').annotate(odd=F('id') + F('name'))
        )

        with self.assertRaises(FieldError) as caught:
            declaration.definition

        self.assertIn('output_field', str(caught.exception))


class GeneratedModelTestCase(SimpleTestCase):
    def test_the_model_reads_the_view_unmanaged(self):
        """
        Case: The model generated from a queryset declaration.
        Expected: Unmanaged, named after the view's identifier, with the compiled columns as its fields in order.
        """
        model = declare('Uppercased', queryset=cake_names).model

        self.assertEqual(model._meta.db_table, 'example_uppercased')
        self.assertFalse(model._meta.managed)
        self.assertEqual([field.name for field in model._meta.fields], ['id', 'name'])

    def test_the_model_is_built_once_and_a_subclass_gets_its_own(self):
        """
        Case: Two accesses on one declaration, and an access on a concrete subclass.
        Expected: The same object twice, and a different one for the subclass (whose identifier differs).
        """
        declaration = declare('Uppercased', queryset=cake_names)
        subclass = type(declaration)('Lowercased', (declaration,), {'app_label': APP_LABEL})

        self.assertIs(declaration.model, declaration.model)
        self.assertIsNot(subclass.model, declaration.model)
        self.assertEqual(subclass.model._meta.db_table, 'example_lowercased')

    def test_objects_queries_the_view(self):
        """
        Case: A queryset built through the declaration's manager.
        Expected: It selects from the view's identifier, so the declaration serves queries as well as migrations.
        """
        declaration = declare('Uppercased', queryset=cake_names)

        self.assertIn('"example_uppercased"', str(declaration.objects.filter(name='lemon').query))

    def test_the_model_stays_out_of_the_global_registry(self):
        """
        Case: The app registry and migration state after a model has been built.
        Expected: It is untouched, so makemigrations never sees a table to create for what is really a view.
        """
        models_before = set(apps.get_models())

        declare('Uppercased', queryset=cake_names).model

        self.assertEqual(set(apps.get_models()), models_before)
        self.assertNotIn(('example', 'uppercased'), ProjectState.from_apps(apps).models)

    def test_the_model_says_it_is_generated(self):
        """
        Case: The generated class seen in a traceback or a debugger.
        Expected: Its qualified name points back at the declaration.
        """
        model = declare('Uppercased', queryset=cake_names).model

        self.assertEqual(model.__qualname__, 'Uppercased.model')

    def test_an_id_column_is_the_default_primary_key(self):
        """
        Case: A queryset selecting an id column, with nothing else declared.
        Expected: id is the primary key.
        """
        model = declare('Uppercased', queryset=cake_names).model

        self.assertEqual(model._meta.pk.name, 'id')

    def test_a_single_column_unique_index_is_the_primary_key(self):
        """
        Case: A materialized view with a one-column unique index and no id column.
        Expected: That column is the primary key.
        """
        model = declare(
            'Totals',
            base=MaterializedView,
            unique_index=('name',),
            queryset=lambda: Cake.objects.values('name').annotate(cakes=Count('id')),
        ).model

        self.assertEqual(model._meta.pk.name, 'name')

    def test_an_explicit_primary_key_wins(self):
        """
        Case: A declaration naming its primary_key outright, beside a unique index saying otherwise.
        Expected: The explicit choice is the primary key.
        """
        model = declare(
            'Totals',
            base=MaterializedView,
            unique_index=('name',),
            primary_key='cakes',
            queryset=lambda: Cake.objects.values('name').annotate(cakes=Count('id')),
        ).model

        self.assertEqual(model._meta.pk.name, 'cakes')

    def test_no_derivable_primary_key_is_refused(self):
        """
        Case: No explicit key, no unique index, no id column.
        Expected: TypeError listing the columns to pick from.
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects.values('name'))

        with self.assertRaises(TypeError) as caught:
            declaration.model

        self.assertIn('primary_key', str(caught.exception))
        self.assertIn('name', str(caught.exception))

    def test_a_primary_key_the_queryset_does_not_select_is_refused(self):
        """
        Case: primary_key naming a column that is not among the compiled ones.
        Expected: TypeError.
        """
        declaration = declare('Uppercased', primary_key='nope', queryset=cake_names)

        with self.assertRaises(TypeError) as caught:
            declaration.model

        self.assertIn("'nope'", str(caught.exception))

    def test_an_id_column_is_not_an_auto_field(self):
        """
        Case: The id column of a source table, whose model field is an AutoField.
        Expected: It is a plain integer field: a view fills nothing in on insert, and an auto field would drag
                  primary_key=True along with it wherever it goes.
        """
        model = declare('Uppercased', queryset=cake_names).model

        self.assertEqual(model._meta.get_field('id').get_internal_type(), 'IntegerField')

    def test_a_generated_column_becomes_its_output_type(self):
        """
        Case: A queryset selecting a GeneratedField column.
        Expected: The model field is the generation's output type, since the view holds the computed value and the
                  expression only means anything on the model it was declared on.
        """
        model = declare('Uppercased', queryset=lambda: Cake.objects.values('id', 'name_uppercased')).model

        field = model._meta.get_field('name_uppercased')
        self.assertEqual(field.get_internal_type(), 'TextField')

    def test_a_foreign_key_column_becomes_its_target_type(self):
        """
        Case: A queryset selecting a foreign key column.
        Expected: The model field is the plain type of the key, not a relation, which could never resolve inside the
                  model's private registry.
        """
        model = declare('Perms', queryset=lambda: Permission.objects.values('id', 'content_type')).model

        field = model._meta.get_field('content_type')
        self.assertFalse(field.is_relation)
        self.assertEqual(field.get_internal_type(), 'IntegerField')

    def test_a_foreign_key_to_a_child_model_resolves_to_the_concrete_key(self):
        """
        Case: A column whose output field is a foreign key to a multi-table-inheritance child, whose primary key is
              the parent link rather than a column of its own. A Subquery over the key is the shape that keeps the
              foreign key as the output field.
        Expected: The model field is the plain type of the concrete key at the end of the chain, dereferenced through
                  the parent link rather than left a relation the private registry could never resolve.
        """
        model = declare(
            'Orders',
            queryset=lambda: BundtOrder.objects.values('id').annotate(
                latest_bundt=Subquery(BundtOrder.objects.values('bundt')[:1])
            ),
        ).model

        field = model._meta.get_field('latest_bundt')
        self.assertFalse(field.is_relation)
        self.assertEqual(field.get_internal_type(), 'IntegerField')
        self.assertEqual(field.db_type(connection), 'integer')

    def test_a_column_shadowing_a_model_attribute_is_refused(self):
        """
        Case: A column named objects.
        Expected: TypeError naming the column (collision with the built-in manager accessor).
        """
        declaration = declare('Uppercased', queryset=lambda: Cake.objects.values('id').annotate(objects=Value(1)))

        with self.assertRaises(TypeError) as caught:
            declaration.model

        self.assertIn("'objects'", str(caught.exception))

    def test_a_raw_sql_view_has_no_model(self):
        """
        Case: .model and .objects on a raw-sql declaration.
        Expected: TypeError pointing at a queryset or a hand-written model.
        """
        declaration = declare('Uppercased', sql='SELECT id, name FROM example_cake')

        for attribute in ('model', 'objects'):
            with self.assertRaises(TypeError) as caught:
                getattr(declaration, attribute)

            self.assertIn('queryset', str(caught.exception))

    def test_a_row_survives_pickling(self):
        """
        Case: Pickle a row of a generated model, as a cache or a task queue would.
        Expected: It comes back equal.
        """
        row = PickledCakes.model(id=1, name='lemon')

        restored = pickle.loads(pickle.dumps(row))

        self.assertIs(type(restored), PickledCakes.model)
        self.assertEqual((restored.id, restored.name), (1, 'lemon'))

    def test_a_declared_index_must_use_selected_columns(self):
        """
        Case: A materialized view whose unique_index names a column the queryset does not select.
        Expected: TypeError at definition time listing the columns.
        """
        declaration = declare(
            'Totals',
            base=MaterializedView,
            unique_index=('nope',),
            queryset=lambda: Cake.objects.values('name').annotate(cakes=Count('id')),
        )

        with self.assertRaises(TypeError) as caught:
            declaration.definition

        self.assertIn("'nope'", str(caught.exception))
        self.assertIn('name', str(caught.exception))


class SerializedDefinitionTestCase(SimpleTestCase):
    def test_a_compiled_definition_serializes_self_contained(self):
        """
        Case: Serialize a compiled definition the way a migration file would.
        Expected: The compiled SELECT spelled out and no reference back to the declaring module or its models, so
                  editing the declaration later cannot rewrite what has already been applied.
        """
        definition = declare('Uppercased', queryset=cake_names).definition

        source, imports = MigrationWriter.serialize(definition)

        self.assertIn('"example_cake"."name"', source)
        self.assertNotIn('example.models', source)
        self.assertNotIn('Cake', source)
        self.assertNotIn('queryset', source)
        self.assertIn('import postgres_objects.views', imports.pop())


class QuerysetViewOperationTestCase(TransactionTestCase):
    available_apps = ['django.contrib.auth', 'django.contrib.contenttypes', 'postgres_objects', 'example']

    def setUp(self):
        super().setUp()
        self._created = []
        self.addCleanup(self._drop_created_views)

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

    def test_a_created_view_is_read_back_through_objects(self):
        """
        Case: Create a view from a compiled queryset, then query it through the declaration's manager.
        Expected: The same rows the source table holds, so one declaration serves the migration and the reads.
        """
        declaration = declare('Kinds', queryset=lambda: ContentType.objects.values('id', 'app_label', 'model'))

        self.apply(AddView(declaration.definition))

        expected = set(ContentType.objects.values_list('app_label', 'model'))
        self.assertTrue(expected)
        self.assertEqual(set(declaration.objects.values_list('app_label', 'model')), expected)

    def test_an_inlined_filter_holds_up_in_the_database(self):
        """
        Case: A view compiled from a queryset filtering on strings carrying a percent and a quote.
        Expected: Postgres accepts the CREATE and the view filters correctly, proving the literals were inlined rather
                  than mangled.
        """
        declaration = declare(
            'Kinds',
            queryset=lambda: ContentType.objects.values('id', 'app_label').filter(
                app_label__startswith='content', model__contains="'"
            ),
        )
        control = declare(
            'Uppercased',
            queryset=lambda: ContentType.objects.values('id', 'app_label').filter(app_label__startswith='content'),
        )

        self.apply(AddView(declaration.definition))
        self.apply(AddView(control.definition))

        self.assertEqual(declaration.objects.count(), 0)
        self.assertEqual(control.objects.count(), ContentType.objects.filter(app_label='contenttypes').count())

    def test_a_compiled_materialized_view_refreshes_concurrently(self):
        """
        Case: A materialized view compiled from an aggregate queryset, declared with the unique index a concurrent
              refresh needs.
        Expected: Created with the index, refreshed concurrently, and read back through the declaration's manager.
        """
        declaration = declare(
            'Totals',
            base=MaterializedView,
            unique_index=('app_label',),
            queryset=lambda: ContentType.objects.values('app_label').annotate(kinds=Count('id')),
        )

        self.apply(AddView(declaration.definition))
        self.apply(RefreshMaterializedView(declaration.definition, concurrently=True))

        row = declaration.objects.get(app_label='contenttypes')
        self.assertEqual(row.kinds, ContentType.objects.filter(app_label='contenttypes').count())

    def test_a_refresh_from_code_moves_the_stored_rows(self):
        """
        Case: Change the source table under a compiled materialized view, then call refresh() on the declaration.
        Expected: The stale count before, the new one after, read through the declaration's own manager.
        """
        declaration = declare(
            'Totals',
            base=MaterializedView,
            unique_index=('app_label',),
            queryset=lambda: ContentType.objects.values('app_label').annotate(kinds=Count('id')),
        )
        self.apply(AddView(declaration.definition))
        stale = declaration.objects.get(app_label='contenttypes').kinds

        ContentType.objects.create(app_label='contenttypes', model='cake_of_the_day')
        self.assertEqual(declaration.objects.get(app_label='contenttypes').kinds, stale)

        declaration.refresh()

        self.assertEqual(declaration.objects.get(app_label='contenttypes').kinds, stale + 1)

    def test_a_refresh_calls_the_queryset_at_most_once(self):
        """
        Case: A refresh from code, with the declaration's queryset wrapped in a call counter.
        Expected: One call, the routing decision in db_for_refresh. The refresh statement itself needs only the
                  identifier and the unique index, so nothing else compiles or touches the queryset on this hot path.
        """
        calls = []

        def counted():
            calls.append(1)
            return ContentType.objects.values('id', 'app_label', 'model')

        declaration = declare('Kinds', base=MaterializedView, queryset=counted)
        self.apply(AddView(declaration.definition))
        calls.clear()

        declaration.refresh()

        self.assertEqual(len(calls), 1)

    def test_reversing_drops_a_compiled_view(self):
        """
        Case: Reverse the AddView of a compiled definition.
        Expected: Gone, so an unapplied migration leaves nothing behind for either body flavor.
        """
        declaration = declare('Kinds', queryset=lambda: ContentType.objects.values('id', 'app_label'))

        self.apply(AddView(declaration.definition))
        self.assertEqual(self.relkind('example_kinds'), 'v')

        self.apply(AddView(declaration.definition), backwards=True)
        self.assertIsNone(self.relkind('example_kinds'))
