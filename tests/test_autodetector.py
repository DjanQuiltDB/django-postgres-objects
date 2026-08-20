import warnings
from unittest import mock

from django.core.management.commands import makemigrations, migrate
from django.db import models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.exceptions import CircularDependencyError
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.state import ModelState, ProjectState
from django.db.models import F
from django.test import SimpleTestCase, override_settings

from postgres_objects import Function, GeneratedField, MaterializedView, View
from postgres_objects.autodetector import (
    DeclarativeObjectAutodetector,
    DeclarativeObjectAutodetectorMixin,
    MigratedObject,
    UnmigratedAppWarning,
    build_migration,
    compose,
    get_autodetector,
    get_migrated_objects,
    get_object_changes,
    get_ordered_nodes,
    patch_migrations,
)
from postgres_objects.operations import (
    AddFunction,
    AddView,
    AlterFunction,
    RecalculateGeneratedField,
    RefreshMaterializedView,
    RemoveFunction,
    RemoveView,
)

MODULE_PATH = 'db_functions'


def declare(class_name, **attrs):
    namespace = {
        'app_label': 'example',
        'arguments': 'input TEXT',
        'returns': 'TEXT',
        'body': 'BEGIN RETURN input; END;',
        'output_field': models.TextField(),
    }
    namespace.update(attrs)

    return type(Function)(class_name, (Function,), namespace)


def declare_view(class_name, **attrs):
    namespace = {'app_label': 'example', 'sql': 'SELECT id FROM example_cake'}
    namespace.update(attrs)

    return type(View)(class_name, (View,), namespace)


def build_graph(migrations):
    """
    Build a migration graph from {(app_label, name): (migration, [dependencies])}.
    """
    graph = MigrationGraph()
    for node, (migration, _) in migrations.items():
        graph.add_node(node, migration)
    for node, (_, dependencies) in migrations.items():
        for dependency in dependencies:
            graph.add_dependency(migrations[node][0], node, dependency)

    return graph


def migration_with(app_label, name, *operations, dependencies=()):
    return {(app_label, name): (build_migration(app_label, name, list(operations)), list(dependencies))}


class GetOrderedNodesTestCase(SimpleTestCase):
    """
    Pins the properties get_ordered_nodes relies on from MigrationGraph._generate_plan, which is private API.
    """

    def test_a_node_follows_the_ones_it_depends_on(self):
        """
        Case: A chain of migrations, each depending on the one before it.
        Expected: They come back in applied order, which is what makes folding the operations reproduce the state the
                  migrations left behind.
        """
        nodes = {}
        nodes.update(migration_with('example', '0001'))
        nodes.update(migration_with('example', '0002', dependencies=[('example', '0001')]))
        nodes.update(migration_with('example', '0003', dependencies=[('example', '0002')]))

        ordered = get_ordered_nodes(build_graph(nodes))

        self.assertEqual(ordered, [('example', '0001'), ('example', '0002'), ('example', '0003')])

    def test_every_node_appears_exactly_once(self):
        """
        Case: Two leaves sharing an ancestor.
        Expected: The shared ancestor is listed once, so folding operations over the result cannot double-count.
        """
        nodes = {}
        nodes.update(migration_with('example', '0001'))
        nodes.update(migration_with('example', '0002', dependencies=[('example', '0001')]))
        nodes.update(migration_with('other', '0001', dependencies=[('example', '0001')]))

        ordered = get_ordered_nodes(build_graph(nodes))

        self.assertEqual(len(ordered), 3)
        self.assertEqual(ordered.index(('example', '0001')), 0)


class GetMigratedObjectsTestCase(SimpleTestCase):
    def test_an_addition_is_recorded(self):
        """
        Case: A graph containing one AddFunction.
        Expected: The object is recorded under its app and name, since there is no ProjectState to read it from.
        """
        definition = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(definition)))

        self.assertEqual(
            get_migrated_objects(graph),
            {('example', 'function', 'doubled'): MigratedObject(definition, {}, ('example', '0001'))},
        )

    def test_a_later_alteration_wins(self):
        """
        Case: An addition followed by an alteration of the same object.
        Expected: The altered definition is what the migrations are considered to have left behind.
        """
        first = declare('Doubled').definition
        second = declare('Doubled', body='BEGIN RETURN input || input; END;').definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(first)))
        nodes.update(
            migration_with('example', '0002', AlterFunction(second, first), dependencies=[('example', '0001')])
        )

        self.assertEqual(
            get_migrated_objects(build_graph(nodes)),
            {('example', 'function', 'doubled'): MigratedObject(second, {}, ('example', '0002'))},
        )

    def test_a_removal_drops_it(self):
        """
        Case: An addition followed by a removal of the same object.
        Expected: Nothing is recorded, so redeclaring it later reads as a fresh addition.
        """
        definition = declare('Doubled').definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(definition)))
        nodes.update(migration_with('example', '0002', RemoveFunction(definition), dependencies=[('example', '0001')]))

        self.assertEqual(get_migrated_objects(build_graph(nodes)), {})

    def test_a_superseding_pair_leaves_the_new_definition_recorded(self):
        """
        Case: An addition, then the written result of a signature change: the new overload added up front and the old
              one removed after the model migrations, all under one name.
        Expected: The fold records the new definition. The trailing removal names the old overload only, so it must
                  not erase what the leading addition recorded.
        """
        old = declare('Doubled').definition
        new = declare('Doubled', arguments='input TEXT, suffix TEXT').definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(old)))
        nodes.update(migration_with('example', '0002', AddFunction(new), dependencies=[('example', '0001')]))
        nodes.update(migration_with('example', '0003', RemoveFunction(old), dependencies=[('example', '0002')]))

        self.assertEqual(
            get_migrated_objects(build_graph(nodes)),
            {('example', 'function', 'doubled'): MigratedObject(new, {}, ('example', '0002'))},
        )

    def test_the_hints_are_carried_along(self):
        """
        Case: An operation written with routing hints.
        Expected: The hints are recorded, so a later change of placement is detectable.
        """
        definition = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(definition, hints={'target': 'ovens'})))

        self.assertEqual(
            get_migrated_objects(graph),
            {('example', 'function', 'doubled'): MigratedObject(definition, {'target': 'ovens'}, ('example', '0001'))},
        )

    def test_an_object_belongs_to_the_app_whose_migration_created_it(self):
        """
        Case: An operation sitting in another app's migration.
        Expected: The object is keyed by that app, not by the one the declaration names.
        """
        definition = declare('Doubled').definition
        graph = build_graph(migration_with('other', '0001', AddFunction(definition)))

        self.assertEqual(list(get_migrated_objects(graph)), [('other', 'function', 'doubled')])

    def test_unrelated_operations_are_ignored(self):
        """
        Case: A migration containing ordinary Django operations.
        Expected: Nothing is recorded, so folding the graph is safe on any project's migrations.
        """
        graph = build_graph(migration_with('example', '0001', mock.Mock()))

        self.assertEqual(get_migrated_objects(graph), {})

    def test_a_recalculation_is_ignored(self):
        """
        Case: A graph containing a RecalculateGeneratedField.
        Expected: Nothing is recorded, since the operation changes stored values without changing any definition.
        """
        graph = build_graph(migration_with('example', '0001', RecalculateGeneratedField('GenModel', 'name_uppercased')))

        self.assertEqual(get_migrated_objects(graph), {})


class GetObjectChangesTestCase(SimpleTestCase):
    def changes(self, declared, graph):
        with mock.patch(
            'postgres_objects.autodetector.changes.get_declarations',
            return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
        ):
            return get_object_changes(graph)[:2]

    def test_a_new_declaration_is_added_before_the_model_migrations(self):
        """
        Case: A declaration no migration has created yet.
        Expected: An AddFunction in the leading set, so the object exists before any model that uses it.
        """
        declaration = declare('Doubled')

        leading, trailing = self.changes([declaration], MigrationGraph())

        self.assertEqual(len(leading['example']), 1)
        self.assertIsInstance(leading['example'][0], AddFunction)
        self.assertEqual(leading['example'][0].definition, declaration.definition)
        self.assertEqual(trailing, {})

    def test_an_unchanged_declaration_produces_nothing(self):
        """
        Case: A declaration matching what the migrations already created.
        Expected: No operations at all, which is what keeps makemigrations --check quiet in CI.
        """
        declaration = declare('Doubled')
        graph = build_graph(migration_with('example', '0001', AddFunction(declaration.definition)))

        self.assertEqual(self.changes([declaration], graph), ({}, {}))

    def test_a_changed_body_alters_in_place(self):
        """
        Case: An edited body, with the signature and return type untouched.
        Expected: A single AlterFunction carrying the previous definition, so the change is reversible.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', body='BEGIN RETURN input || input; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], AlterFunction)
        self.assertEqual(leading['example'][0].previous, previous)
        self.assertEqual(trailing, {})

    def test_a_changed_return_type_drops_then_adds_up_front(self):
        """
        Case: Same signature, different return type.
        Expected: Both steps run before the model migrations, since the two cannot coexist.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', returns='INT')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], RemoveFunction)
        self.assertIsInstance(leading['example'][1], AddFunction)
        self.assertEqual(trailing, {})

    def test_a_changed_signature_adds_first_and_removes_after(self):
        """
        Case: A new argument list, so the two coexist as overloads.
        Expected: The new one is created up front and the old one removed only after the model migrations, leaving a
                  migration in between for dependents to move across.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', arguments='input TEXT, suffix TEXT')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], AddFunction)
        self.assertIsInstance(trailing['example'][0], RemoveFunction)
        self.assertEqual(trailing['example'][0].definition, previous)

    def test_a_written_signature_change_detects_nothing_on_the_next_run(self):
        """
        Case: The declaration is unchanged since the run that wrote its signature change as the superseding pair: the
              new overload added up front, the old one removed after the model migrations.
        Expected: Nothing is detected, rather than the new overload being added a second time.
        """
        old = declare('Doubled').definition
        declaration = declare('Doubled', arguments='input TEXT, suffix TEXT')

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(old)))
        nodes.update(
            migration_with('example', '0002', AddFunction(declaration.definition), dependencies=[('example', '0001')])
        )
        nodes.update(migration_with('example', '0003', RemoveFunction(old), dependencies=[('example', '0002')]))

        self.assertEqual(self.changes([declaration], build_graph(nodes)), ({}, {}))

    def test_changed_hints_drop_and_recreate_up_front(self):
        """
        Case: The declaration is annotated to live somewhere else, its definition untouched.
        Expected: The drop and the create run adjacent, drop first, before the model migrations. The superseding shape
                  would be destructive here: on any connection both hint sets allow, its trailing drop names the very
                  function the leading create just wrote.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', router_hints={'target': 'ovens'})
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], RemoveFunction)
        self.assertEqual(leading['example'][0].hints, {})
        self.assertIsInstance(leading['example'][1], AddFunction)
        self.assertEqual(leading['example'][1].hints, {'target': 'ovens'})
        self.assertEqual(trailing, {})

    def test_changed_hints_with_an_altered_body_drop_and_recreate_up_front(self):
        """
        Case: The body edited and the placement annotation changed in the same run.
        Expected: The same adjacent drop-and-recreate, the removal carrying the old definition and hints and the
                  creation the new ones, so each connection ends up with exactly what its hints allow.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', body='BEGIN RETURN input || input; END;', router_hints={'target': 'ovens'})
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], RemoveFunction)
        self.assertEqual(leading['example'][0].definition, previous)
        self.assertEqual(leading['example'][0].hints, {})
        self.assertIsInstance(leading['example'][1], AddFunction)
        self.assertEqual(leading['example'][1].definition, declaration.definition)
        self.assertEqual(leading['example'][1].hints, {'target': 'ovens'})
        self.assertEqual(trailing, {})

    def test_a_written_hints_change_detects_nothing_on_the_next_run(self):
        """
        Case: The graph holds the written result of a hints change: the removal of the old copy followed by the creation
              under the new hints, adjacent in one leading migration.
        Expected: A second run detects nothing.
        """
        declaration = declare('Doubled', router_hints={'target': 'ovens'})
        previous = declare('Doubled').definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(previous)))
        nodes.update(
            migration_with(
                'example',
                '0002',
                RemoveFunction(previous),
                AddFunction(declaration.definition, hints={'target': 'ovens'}),
                dependencies=[('example', '0001')],
            )
        )

        self.assertEqual(self.changes([declaration], build_graph(nodes)), ({}, {}))

    def test_a_default_only_argument_change_alters_in_place(self):
        """
        Case: A parameter gains a default clause, nothing else changes.
        Expected: A single leading AlterFunction and no trailing operation, since Postgres still sees the same function.
                  This is the whole pipeline's view of what plan_change_from decides.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', arguments="input TEXT DEFAULT ''")
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], AlterFunction)
        self.assertEqual(trailing, {})

    def test_a_dropped_declaration_is_removed_after_the_model_migrations(self):
        """
        Case: A declaration deleted from the functions module.
        Expected: A RemoveFunction in the trailing set, so it is dropped only once the model migrations have taken every
                  dependent off it.
        """
        previous = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([], graph)

        self.assertEqual(leading, {})
        self.assertIsInstance(trailing['example'][0], RemoveFunction)

    def test_a_rename_with_a_pinned_db_name_is_not_a_drop(self):
        """
        Case: The declaration is renamed in Python while keeping the identifier it was created under.
        Expected: It is treated as the same object, so the live one is not dropped and recreated.
        """
        previous = declare('OldName', db_name='pinned').definition
        declaration = declare('NewName', db_name='pinned')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertEqual(trailing, {})
        self.assertIsInstance(leading['example'][0], AlterFunction)

    def test_a_written_rename_is_not_swept_as_a_drop_on_the_next_run(self):
        """
        Case: The graph holds the written result of a pinned-db_name rename: the AddFunction under the old name
              followed by the AlterFunction recorded under the new one.
        Expected: A second run detects nothing.
        """
        previous = declare('OldName', db_name='pinned').definition
        declaration = declare('NewName', db_name='pinned')

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(previous)))
        nodes.update(
            migration_with(
                'example', '0002', AlterFunction(declaration.definition, previous), dependencies=[('example', '0001')]
            )
        )

        self.assertEqual(self.changes([declaration], build_graph(nodes)), ({}, {}))

    def test_a_rename_matches_the_same_apps_object_first(self):
        """
        Case: Two apps' migrations each created a function under the pinned identifier (each on its own database, as
              routing hints allow), and the declaration in one of them is renamed.
        Expected: The rename is matched to the declaring app's own record rather than to whichever app the fold
                  happened to visit last, and no drop of the identifier is emitted for the other record either.
        """
        example_previous = declare('OldName', db_name='pinned').definition
        other_previous = declare('OtherOld', db_name='pinned', app_label='other').definition
        declaration = declare('NewName', db_name='pinned')

        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(example_previous)))
        nodes.update(migration_with('other', '0001', AddFunction(other_previous)))

        leading, trailing = self.changes([declaration], build_graph(nodes))

        self.assertIsInstance(leading['example'][0], AlterFunction)
        self.assertEqual(leading['example'][0].previous, example_previous)
        self.assertEqual(trailing, {})

    def test_a_rename_is_not_assumed_when_the_old_name_is_still_declared(self):
        """
        Case: Two declarations share a db_name, one of them the previously migrated name.
        Expected: The still-declared one is not treated as having been renamed away.
        """
        previous = declare('OldName', db_name='pinned').definition
        old = declare('OldName', db_name='pinned')
        new = declare('NewName', db_name='pinned')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([old, new], graph)

        self.assertIsInstance(leading['example'][0], AddFunction)
        self.assertEqual(trailing, {})


class ViewPlacementTestCase(SimpleTestCase):
    """
    Views are placed the other way round from functions, which is the whole of what the second kind of object needed
    from the change detection.
    """

    def changes(self, declared, graph):
        with mock.patch(
            'postgres_objects.autodetector.changes.get_declarations',
            return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
        ):
            return get_object_changes(graph)[:2]

    def test_a_new_view_is_created_after_the_model_migrations(self):
        """
        Case: A view no migration has created yet.
        Expected: An AddView in the trailing set, since the table it selects from only exists once the model migrations
                  have run.
        """
        declaration = declare_view('Uppercased')

        leading, trailing = self.changes([declaration], MigrationGraph())

        self.assertEqual(leading, {})
        self.assertIsInstance(trailing['example'][0], AddView)

    def test_a_dropped_view_is_removed_before_the_model_migrations(self):
        """
        Case: A view deleted from the views module.
        Expected: A RemoveView in the leading set, so it is gone before a column it reads can be altered away.
        """
        previous = declare_view('Uppercased').definition
        graph = build_graph(migration_with('example', '0001', AddView(previous)))

        leading, trailing = self.changes([], graph)

        self.assertIsInstance(leading['example'][0], RemoveView)
        self.assertEqual(trailing, {})

    def test_a_changed_view_is_dropped_before_and_recreated_after(self):
        """
        Case: An edited SELECT.
        Expected: The drop leads and the create trails, so the model migrations run in between with the view out of the
                  way. There is no in-place replace.
        """
        previous = declare_view('Uppercased').definition
        declaration = declare_view('Uppercased', sql='SELECT id, name FROM example_cake')
        graph = build_graph(migration_with('example', '0001', AddView(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], RemoveView)
        self.assertEqual(leading['example'][0].definition, previous)
        self.assertIsInstance(trailing['example'][0], AddView)
        self.assertEqual(trailing['example'][0].definition, declaration.definition)

    def test_a_views_changed_hints_drop_before_and_recreate_after(self):
        """
        Case: A view annotated to live somewhere else, its definition untouched.
        Expected: The straddle, exactly like an edited SELECT: a view's drop side runs before its create side on every
                  connection, so the shape that is destructive for functions is the safe and preferable one here.
        """
        previous = declare_view('Uppercased').definition
        declaration = declare_view('Uppercased', router_hints={'target': 'ovens'})
        graph = build_graph(migration_with('example', '0001', AddView(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertIsInstance(leading['example'][0], RemoveView)
        self.assertEqual(leading['example'][0].hints, {})
        self.assertIsInstance(trailing['example'][0], AddView)
        self.assertEqual(trailing['example'][0].hints, {'target': 'ovens'})

    def test_an_unchanged_view_produces_nothing(self):
        """
        Case: A view matching what the migrations already created.
        Expected: No operations, so a project that changed nothing writes no migration.
        """
        declaration = declare_view('Uppercased')
        graph = build_graph(migration_with('example', '0001', AddView(declaration.definition)))

        self.assertEqual(self.changes([declaration], graph), ({}, {}))

    def test_a_renamed_views_identifier_is_never_swept_as_a_drop(self):
        """
        Case: The graph records the pinned identifier as created under the old name and again under the new one, with
              the removal of the old key not (or no longer) recorded. (This indicates a partially recorded rename.)
        Expected: A second run detects nothing. The sweep must never emit a RemoveView whose DROP names an identifier a
                  live declaration owns.
        """
        previous = declare_view('OldName', db_name='pinned').definition
        declaration = declare_view('NewName', db_name='pinned')

        nodes = {}
        nodes.update(migration_with('example', '0001', AddView(previous)))
        nodes.update(
            migration_with('example', '0002', AddView(declaration.definition), dependencies=[('example', '0001')])
        )

        self.assertEqual(self.changes([declaration], build_graph(nodes)), ({}, {}))

    def test_a_view_and_a_function_of_the_same_name_are_separate_objects(self):
        """
        Case: An app declaring a function and a view under one name.
        Expected: Both are added. Postgres keeps functions and relations in separate namespaces, so conflating them
                  would wrongly read one as a change to the other.
        """
        leading, trailing = self.changes([declare('Totals'), declare_view('Totals')], MigrationGraph())

        self.assertIsInstance(leading['example'][0], AddFunction)
        self.assertIsInstance(trailing['example'][0], AddView)

    def test_view_drops_lead_the_function_operations(self):
        """
        Case: A run dropping a view and adding a function in one app.
        Expected: The view drop comes first in the leading migration, since a view has to be gone before anything it may
                  call is touched.
        """
        previous = declare_view('Uppercased').definition
        graph = build_graph(migration_with('example', '0001', AddView(previous)))

        leading, _ = self.changes([declare('Doubled')], graph)

        self.assertIsInstance(leading['example'][0], RemoveView)
        self.assertIsInstance(leading['example'][1], AddFunction)

    def test_view_creates_follow_the_function_removals(self):
        """
        Case: A run dropping a function and adding a view in one app.
        Expected: The function removal comes first in the trailing migration, mirroring the leading order.
        """
        previous = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        _, trailing = self.changes([declare_view('Uppercased')], graph)

        self.assertIsInstance(trailing['example'][0], RemoveFunction)
        self.assertIsInstance(trailing['example'][1], AddView)

    def test_views_are_dropped_in_reverse_declaration_order(self):
        """
        Case: Two views deleted at once, the second declared on top of the first.
        Expected: The dependent one is dropped first, since Postgres refuses to drop a view another still reads.
        """
        base = declare_view('Base').definition
        stacked = declare_view('Stacked', sql='SELECT id FROM example_base').definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddView(base)))
        nodes.update(migration_with('example', '0002', AddView(stacked), dependencies=[('example', '0001')]))

        leading, _ = self.changes([], build_graph(nodes))

        self.assertEqual([operation.definition for operation in leading['example']], [stacked, base])

    def test_views_are_created_in_declaration_order(self):
        """
        Case: Two new views, the second declared on top of the first.
        Expected: The one being selected from is created first, which is what makes declaring them in order enough.
        """
        base = declare_view('Base')
        stacked = declare_view('Stacked', sql='SELECT id FROM example_base')

        _, trailing = self.changes([base, stacked], MigrationGraph())

        self.assertEqual(
            [operation.definition for operation in trailing['example']], [base.definition, stacked.definition]
        )

    def test_a_view_saying_what_it_reads_is_created_after_it_whatever_the_order(self):
        """
        Case: Two new views, the one being read declared last.
        Expected: The one being read is created first, the other second.
        """
        stacked = declare_view('Stacked', depends_on=['example_base'])
        base = declare_view('Base')

        _, trailing = self.changes([stacked, base], MigrationGraph())

        self.assertEqual(
            [operation.definition for operation in trailing['example']], [base.definition, stacked.definition]
        )

    def test_a_view_saying_what_it_reads_is_dropped_before_it_whatever_the_order(self):
        """
        Case: The same two views deleted at once.
        Expected: The dependent one is dropped first.
        """
        base = declare_view('Base').definition
        stacked = declare_view('Stacked', depends_on=['example_base']).definition

        nodes = {}
        nodes.update(migration_with('example', '0001', AddView(stacked)))
        nodes.update(migration_with('example', '0002', AddView(base), dependencies=[('example', '0001')]))

        leading, _ = self.changes([], build_graph(nodes))

        self.assertEqual([operation.definition for operation in leading['example']], [stacked, base])

    def test_two_views_of_one_app_reading_each_other_are_refused(self):
        """
        Case: Two views in the same app, each declaring that it reads the other.
        Expected: Refused loudly. Falling back to declaration order would write a migration whose operations are
                  silently mis-ordered and fail only at apply time.
        """
        first = declare_view('First', depends_on=['example_second'])
        second = declare_view('Second', depends_on=['example_first'])

        with self.assertRaisesMessage(CircularDependencyError, 'read from each other'):
            self.changes([first, second], MigrationGraph())

    def test_a_reference_to_a_view_of_another_app_does_not_reorder_this_one(self):
        """
        Case: A view reading one another app owns.
        Expected: No reordering (ordering across apps is a migration dependency, not a position in a list).
        """
        first = declare_view('First', depends_on=['other_thing'])
        second = declare_view('Second')

        _, trailing = self.changes([first, second], MigrationGraph())

        self.assertEqual(
            [operation.definition for operation in trailing['example']], [first.definition, second.definition]
        )


class UnmigratedAppWarningTestCase(SimpleTestCase):
    def detect(self, leading, trailing):
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value={}),
            mock.patch(
                'postgres_objects.autodetector.detector.get_object_changes', return_value=(leading, trailing, {})
            ),
        ):
            return autodetector._detect_changes(graph=MigrationGraph())

    def test_an_app_without_a_migrations_package_warns(self):
        """
        Case: Database-object changes land in an app Django's questioner refuses an initial migration for, simulating
              an app with no migrations package.
        Expected: An UnmigratedAppWarning naming the app.
        """
        declaration = declare('Doubled', app_label='homeless')

        with self.assertWarnsRegex(UnmigratedAppWarning, 'homeless'):
            self.detect({'homeless': [AddFunction(declaration.definition)]}, {})

    def test_a_trailing_only_app_without_a_migrations_package_warns(self):
        """
        Case: The unwritable app holds only a trailing removal.
        Expected: An UnmigratedAppWarning naming the app.
        """
        declaration = declare('Doubled', app_label='homeless')

        with self.assertWarnsRegex(UnmigratedAppWarning, 'homeless'):
            self.detect({}, {'homeless': [RemoveFunction(declaration.definition)]})

    def test_an_app_with_a_migrations_package_does_not_warn(self):
        """
        Case: The same change in an app whose migrations package exists, if empty.
        Expected: No warning (Django will write the initial migration for it).
        """
        declaration = declare('Doubled')

        with warnings.catch_warnings():
            warnings.simplefilter('error', UnmigratedAppWarning)
            self.detect({'example': [AddFunction(declaration.definition)]}, {})


class KindConfigurationTestCase(SimpleTestCase):
    def changes(self, declared, graph):
        with mock.patch(
            'postgres_objects.autodetector.changes.get_declarations',
            return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
        ):
            return get_object_changes(graph)[:2]

    @override_settings(POSTGRES_OBJECTS={'FUNCTIONS_MODULE_PATH': 'db_functions'})
    def test_an_unset_views_module_sweeps_no_view(self):
        """
        Case: A migrated view, with only the functions module configured.
        Expected: Nothing happens.
        """
        previous = declare_view('Uppercased').definition
        graph = build_graph(migration_with('example', '0001', AddView(previous)))

        self.assertEqual(self.changes([], graph), ({}, {}))

    @override_settings(POSTGRES_OBJECTS={'VIEWS_MODULE_PATH': 'db_views'})
    def test_an_unset_functions_module_sweeps_no_function(self):
        """
        Case: A migrated function, with only the views module configured.
        Expected: Nothing happens.
        """
        previous = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        self.assertEqual(self.changes([], graph), ({}, {}))

    @override_settings(POSTGRES_OBJECTS={'FUNCTIONS_MODULE_PATH': 'db_functions'})
    def test_a_configured_kind_is_still_swept_while_the_other_is_off(self):
        """
        Case: A migrated function whose declaration was deleted, with the views module unset.
        Expected: The function is still removed after the model migrations; only the unmanaged view is exempt.
        """
        previous = declare('Doubled').definition
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([], graph)

        self.assertEqual(leading, {})
        self.assertIsInstance(trailing['example'][0], RemoveFunction)


def project_state(app_label='example', field=None, extra_fields=()):
    """
    A project state holding one model, optionally carrying a generated column, the way the autodetector receives the
    before and after states.
    """
    fields = [('id', models.AutoField(primary_key=True)), ('name', models.TextField())]
    fields.extend(extra_fields)
    if field is not None:
        fields.append(('name_uppercased', field))

    state = ProjectState()
    state.add_model(ModelState(app_label, 'GenModel', fields))

    return state


def generated(declaration, **kwargs):
    kwargs.setdefault('db_persist', True)

    return GeneratedField(expression=declaration(F('name')), output_field=models.TextField(), **kwargs)


class RecalculationTestCase(SimpleTestCase):
    """
    A stored generated column holds what the function computed when its row was written, so a change to what the
    function computes has to be followed by a rewrite of every table that opted in through the wrapper field.
    """

    def changes(self, declared, graph, from_state=None, to_state=None):
        with mock.patch(
            'postgres_objects.autodetector.changes.get_declarations',
            return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
        ):
            return get_object_changes(graph, from_state, to_state)[:2]

    def altered_body(self):
        """
        The migrated declaration and the edited one: same signature, new body.
        """
        previous = declare('Doubled')
        current = declare('Doubled', body='BEGIN RETURN input || input; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))

        return current, graph

    def test_a_changed_body_recalculates_a_dependent_column(self):
        """
        Case: The body of a function called by a wrapper field's expression changes.
        Expected: A RecalculateGeneratedField for that column in the model app's trailing set.
        """
        current, graph = self.altered_body()
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        recalculation = trailing['example'][0]
        self.assertIsInstance(recalculation, RecalculateGeneratedField)
        self.assertEqual((recalculation.model_name, recalculation.name), ('GenModel', 'name_uppercased'))

    def test_the_recalculation_comes_before_other_trailing_operations(self):
        """
        Case: The body change happens in the same run as a function removal, which also trails.
        Expected: The recalculation comes first; a trailing create such as a materialized view snapshots data, so
                  everything downstream has to see recomputed values.
        """
        current, _ = self.altered_body()
        leftover = declare('Leftover')
        nodes = {}
        nodes.update(migration_with('example', '0001', AddFunction(declare('Doubled').definition)))
        nodes.update(
            migration_with('example', '0002', AddFunction(leftover.definition), dependencies=[('example', '0001')])
        )
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], build_graph(nodes), state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)
        self.assertIsInstance(trailing['example'][1], RemoveFunction)

    def test_a_modifier_only_change_recalculates_nothing(self):
        """
        Case: Only the volatility and parallel safety of the function change.
        Expected: No recalculation, since a promise to the planner cannot change a stored value. Rewriting the whole
                  table for one would be a serious surprise.
        """
        previous = declare('Doubled')
        current = declare('Doubled', volatility='IMMUTABLE', parallel='SAFE')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertEqual(trailing, {})

    def test_a_strictness_change_recalculates(self):
        """
        Case: The function's STRICT flag flips, with the body untouched.
        Expected: A recalculation, since STRICT changes what NULL input produces.
        """
        previous = declare('Doubled')
        current = declare('Doubled', strict=True)
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)

    def test_a_returns_only_change_recalculates_nothing(self):
        """
        Case: Only the function's return type changes, its body untouched.
        Expected: No recalculation, since an unchanged body computes unchanged values.
        """
        previous = declare('Doubled')
        current = declare('Doubled', returns='INT')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertEqual([type(operation) for operation in trailing.get('example', [])], [])

    def test_a_replace_with_a_changed_body_recalculates(self):
        """
        Case: The return type and the body change together, which is a drop and recreate.
        Expected: A recalculation.
        """
        previous = declare('Doubled')
        current = declare('Doubled', returns='INT', body='BEGIN RETURN 1; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)

    def test_a_supersede_with_a_changed_body_recalculates_before_the_drop(self):
        """
        Case: The argument list and the body change together, so the new overload supersedes the old one.
        Expected: A recalculation, and it precedes the trailing removal: SET EXPRESSION re-resolves the column's
                  expression, rebinding it onto the new overload so the drop of the old one is no longer blocked.
        """
        previous = declare('Doubled')
        current = declare('Doubled', arguments='input TEXT, suffix TEXT', body='BEGIN RETURN input || suffix; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)
        self.assertIsInstance(trailing['example'][1], RemoveFunction)

    def test_an_explicit_dependency_recalculates_across_a_replace(self):
        """
        Case: A recalculate_on dependant of a function whose return type and body change together.
        Expected: A recalculation. This dependency does not block the replace's drop, so without the rewrite the
                  column would silently keep values the old body computed.
        """
        previous = declare('Doubled')
        current = declare('Doubled', returns='INT', body='BEGIN RETURN 1; END;')
        unrelated = declare('Unrelated')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(unrelated, recalculate_on=('example_doubled',)))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)

    def test_a_body_change_beside_an_added_default_recalculates(self):
        """
        Case: The body changes in the same edit that adds a defaulted parameter.
        Expected: A recalculation. Neither the in-place alter this now plans as, nor the supersede the raw signature
                  comparison used to plan, rewrites the column by itself: its expression deconstructs identically.
        """
        previous = declare('Doubled')
        current = declare('Doubled', arguments="input TEXT DEFAULT ''", body='BEGIN RETURN input || input; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous.definition)))
        state = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)

    def test_a_plain_generated_field_is_left_alone(self):
        """
        Case: The model uses Django's own GeneratedField rather than the wrapper.
        Expected: No recalculation. The rewrite is opt-in, since it takes an exclusive lock on the whole table.
        """
        current, graph = self.altered_body()
        field = models.GeneratedField(expression=current(F('name')), output_field=models.TextField(), db_persist=True)
        state = project_state(field=field)

        _, trailing = self.changes([current], graph, state, state)

        self.assertEqual(trailing, {})

    def test_a_virtual_column_is_left_alone(self):
        """
        Case: A wrapper field that is not stored.
        Expected: No recalculation, since values computed on read can never go stale.
        """
        current, graph = self.altered_body()
        state = project_state(field=generated(current, db_persist=False))

        _, trailing = self.changes([current], graph, state, state)

        self.assertEqual(trailing, {})

    def test_an_opted_out_column_is_left_alone(self):
        """
        Case: A wrapper field carrying recalculate=False, whose expression does call the changed function.
        Expected: No recalculation.
        """
        current, graph = self.altered_body()
        state = project_state(field=generated(current, recalculate=False))

        _, trailing = self.changes([current], graph, state, state)

        self.assertEqual(trailing, {})

    def test_an_explicit_dependency_recalculates(self):
        """
        Case: The column's expression does not call the changed function; recalculate_on names it, standing in for a
              function called from inside another function's body.
        Expected: A recalculation, since the expression cannot show this dependency itself.
        """
        current, graph = self.altered_body()
        unrelated = declare('Unrelated')
        state = project_state(field=generated(unrelated, recalculate_on=('example_doubled',)))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['example'][0], RecalculateGeneratedField)

    def test_a_column_being_added_is_not_recalculated(self):
        """
        Case: The column does not exist in the previous state; it is being added in this very run.
        Expected: No recalculation, since the ADD COLUMN computes every row with the new body already.
        """
        current, graph = self.altered_body()
        before = project_state()
        after = project_state(field=generated(current))

        _, trailing = self.changes([current], graph, before, after)

        self.assertEqual(trailing, {})

    def test_a_column_being_removed_is_not_recalculated(self):
        """
        Case: The column is in the previous state but gone from the new one.
        Expected: No recalculation, since there is nothing left to recompute once the model migrations have run.
        """
        current, graph = self.altered_body()
        before = project_state(field=generated(current))
        after = project_state()

        _, trailing = self.changes([current], graph, before, after)

        self.assertEqual(trailing, {})

    def test_the_recalculation_lands_in_the_models_app(self):
        """
        Case: The function is declared in one app and the model lives in another.
        Expected: The recalculation is keyed by the model's app, whose trailing migration is what waits for every other
                  app's object migrations.
        """
        current, graph = self.altered_body()
        state = project_state(app_label='other', field=generated(current))

        _, trailing = self.changes([current], graph, state, state)

        self.assertIsInstance(trailing['other'][0], RecalculateGeneratedField)
        self.assertNotIn('example', trailing)

    def test_without_states_no_recalculation_is_attempted(self):
        """
        Case: get_object_changes called with the graph alone, as older callers do.
        Expected: The change detection works as before, with no recalculations.
        """
        current, graph = self.altered_body()

        leading, trailing = self.changes([current], graph)

        self.assertIsInstance(leading['example'][0], AlterFunction)
        self.assertEqual(trailing, {})


class SplicingTestCase(SimpleTestCase):
    def detect(self, leading, trailing, changes):
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value=changes),
            mock.patch(
                'postgres_objects.autodetector.detector.get_object_changes', return_value=(leading, trailing, {})
            ),
        ):
            return autodetector._detect_changes(graph=MigrationGraph())

    def test_the_object_migration_goes_in_front_and_the_model_migration_depends_on_it(self):
        """
        Case: An app with both a new declaration and detected model changes.
        Expected: The object migration is inserted first and the model migration is made to depend on it.
        """
        model_migration = build_migration('example', 'auto_1', [])
        changes = {'example': [model_migration]}

        result = self.detect({'example': [AddFunction(declare('Doubled').definition)]}, {}, changes)

        self.assertEqual(result['example'][0].name, 'auto_db_objects')
        self.assertIs(result['example'][1], model_migration)
        self.assertIn(('example', 'auto_db_objects'), model_migration.dependencies)

    def test_another_apps_model_migration_also_waits_for_it(self):
        """
        Case: One app declares a function, another app's model uses it.
        Expected: The other app's first migration depends on the declaring app's object migration.
        """
        other_migration = build_migration('other', 'auto_1', [])
        changes = {'example': [build_migration('example', 'auto_1', [])], 'other': [other_migration]}

        self.detect({'example': [AddFunction(declare('Doubled').definition)]}, {}, changes)

        self.assertIn(('example', 'auto_db_objects'), other_migration.dependencies)

    def test_a_removal_waits_for_every_apps_last_migration(self):
        """
        Case: A removal alongside model changes in more than one app.
        Expected: The removal migration depends on every app's last migration, since any of them could have been the
                  last thing referencing the object.
        """
        changes = {
            'example': [build_migration('example', 'auto_1', [])],
            'other': [build_migration('other', '0007', [])],
        }

        result = self.detect({}, {'example': [RemoveFunction(declare('Doubled').definition)]}, changes)

        removal = result['example'][-1]
        self.assertEqual(removal.name, 'auto_db_objects_last')
        self.assertIn(('other', '0007'), removal.dependencies)

    def test_an_app_with_no_model_changes_still_gets_its_object_migration(self):
        """
        Case: A declaration changed in an app whose models did not change at all.
        Expected: A migration is still created for it, rather than the change being dropped for want of somewhere to put
                  it.
        """
        result = self.detect({'example': [AddFunction(declare('Doubled').definition)]}, {}, {})

        self.assertEqual(len(result['example']), 1)
        self.assertEqual(result['example'][0].name, 'auto_db_objects')

    def test_a_recalculation_waits_for_another_apps_function_change(self):
        """
        Case: A body-only change to a function in one app recalculates a column in another, with no model changes
              anywhere.
        Expected: The recalculating app's trailing migration depends on the function app's object migration, so the
                  rewrite always runs after the new body is in place.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', body='BEGIN RETURN input || input; END;').definition

        result = self.detect(
            {'example': [AlterFunction(current, previous)]},
            {'other': [RecalculateGeneratedField('GenModel', 'name_uppercased')]},
            {},
        )

        recalculation = result['other'][-1]
        self.assertEqual(recalculation.name, 'auto_db_objects_last')
        self.assertIn(('example', 'auto_db_objects'), recalculation.dependencies)


class CrossAppDependencyTestCase(SimpleTestCase):
    def detect(self, leading, trailing, changes, graph=None):
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value=changes),
            mock.patch(
                'postgres_objects.autodetector.detector.get_object_changes', return_value=(leading, trailing, {})
            ),
        ):
            return autodetector._detect_changes(graph=graph or MigrationGraph())

    def test_a_view_waits_for_the_app_whose_view_it_creates(self):
        """
        Case: Two apps declaring a view in one run, one of them reading the other's.
        Expected: The reading app's trailing migration depends on the other's.
        """
        base = declare_view('Base')
        stacked = declare_view('Stacked', app_label='other', depends_on=['example_base'])

        result = self.detect({}, {'example': [AddView(base.definition)], 'other': [AddView(stacked.definition)]}, {})

        self.assertIn(('example', 'auto_db_objects_last'), result['other'][-1].dependencies)
        self.assertNotIn(('other', 'auto_db_objects_last'), result['example'][-1].dependencies)

    def test_a_view_waits_for_the_migration_that_created_what_it_reads(self):
        """
        Case: A view reading one an earlier run already created in another app.
        Expected: A dependency on that exact migration.
        """
        base = declare_view('Base').definition
        stacked = declare_view('Stacked', app_label='other', depends_on=['example_base'])
        graph = build_graph(migration_with('example', '0001', AddView(base)))

        result = self.detect({}, {'other': [AddView(stacked.definition)]}, {}, graph)

        self.assertIn(('example', '0001'), result['other'][-1].dependencies)

    def test_a_view_waits_for_an_untouched_apps_latest_migration(self):
        """
        Case: A view reading a model in an app with no changes in this run.
        Expected: A dependency on that app's latest migration.
        """
        declaration = declare_view('Credits', app_label='other', depends_on=['example_cake'])
        graph = build_graph(migration_with('example', '0005', mock.Mock()))

        result = self.detect({}, {'other': [AddView(declaration.definition)]}, {}, graph)

        self.assertIn(('example', '0005'), result['other'][-1].dependencies)

    def test_a_view_reading_its_own_apps_view_needs_no_dependency(self):
        """
        Case: A view reading another declared in the same app.
        Expected: No dependency added. (The two are ordered inside one migration, and an app cannot wait for itself.)
        """
        base = declare_view('Base')
        stacked = declare_view('Stacked', depends_on=['example_base'])

        result = self.detect({}, {'example': [AddView(base.definition), AddView(stacked.definition)]}, {})

        self.assertNotIn(('example', 'auto_db_objects_last'), result['example'][-1].dependencies)

    def test_dropping_a_view_waits_for_the_app_dropping_what_reads_it(self):
        """
        Case: Two apps dropping a view in one run, one of them reading the other's.
        Expected: The app owning the dependency waits for the app dropping the dependent.
        """
        base = declare_view('Base').definition
        stacked = declare_view('Stacked', app_label='other', depends_on=['example_base']).definition

        result = self.detect({'example': [RemoveView(base)], 'other': [RemoveView(stacked)]}, {}, {})

        self.assertIn(('other', 'auto_db_objects'), result['example'][0].dependencies)
        self.assertNotIn(('example', 'auto_db_objects'), result['other'][0].dependencies)

    def test_a_dependency_on_an_apps_on_disk_migration_is_not_a_cycle(self):
        """
        Case: One app's new view reads the other app's new view, while that other view reads a model table an on-disk
              migration of the first app created, so the only edge between the two trailing migrations points one way.
        Expected: Migrations are produced, wired to the on-disk migration and the trailing one respectively.
        """
        reads_table = declare_view('Base', app_label='other', depends_on=['example_cake'])
        reads_view = declare_view('Stacked', depends_on=['other_base'])
        graph = build_graph(migration_with('example', '0005', mock.Mock()))

        result = self.detect(
            {}, {'other': [AddView(reads_table.definition)], 'example': [AddView(reads_view.definition)]}, {}, graph
        )

        self.assertIn(('example', '0005'), result['other'][-1].dependencies)
        self.assertIn(('other', 'auto_db_objects_last'), result['example'][-1].dependencies)

    def test_two_apps_reading_each_other_are_refused(self):
        """
        Case: Each of two apps declaring a view that reads one of the other's.
        Expected: Refused. (It is legal in Postgres but our code isn't equipped to handle it.)
        """
        trailing = {
            'example': [
                AddView(declare_view('First').definition),
                AddView(declare_view('Second', depends_on=['other_third']).definition),
            ],
            'other': [
                AddView(declare_view('Third', app_label='other').definition),
                AddView(declare_view('Fourth', app_label='other', depends_on=['example_first']).definition),
            ],
        }

        with self.assertRaisesMessage(CircularDependencyError, 'read from each other'):
            self.detect({}, trailing, {})


class CreatorDependencyTestCase(SimpleTestCase):
    def detect(self, declared, graph):
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value={}),
            mock.patch(
                'postgres_objects.autodetector.changes.get_declarations',
                return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
            ),
        ):
            return autodetector._detect_changes(graph=graph)

    def test_a_cross_app_rename_waits_for_the_creator_migration(self):
        """
        Case: A declaration with a pinned db_name moved to another app, its body changed in the same edit.
        Expected: The new app's leading migration depends on the migration that created the function. On a fresh
                  database nothing else orders the two apps, and running the alter first would let the creator's later
                  CREATE OR REPLACE silently revert the body.
        """
        previous = declare('OldName', db_name='shared', app_label='app_old').definition
        declaration = declare(
            'NewName', db_name='shared', app_label='app_new', body='BEGIN RETURN input || input; END;'
        )
        graph = build_graph(migration_with('app_old', '0001', AddFunction(previous)))

        result = self.detect([declaration], graph)

        self.assertEqual(result['app_new'][0].name, 'auto_db_objects')
        self.assertIn(('app_old', '0001'), result['app_new'][0].dependencies)

    def test_a_same_app_rename_gains_no_self_dependency(self):
        """
        Case: The same pinned-db_name rename, staying inside the app whose migration created the function.
        Expected: No dependency on the app's own creator migration; arrange_for_graph already anchors an app's first
                  new migration on its own leaf.
        """
        previous = declare('OldName', db_name='shared').definition
        declaration = declare('NewName', db_name='shared', body='BEGIN RETURN input || input; END;')
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        result = self.detect([declaration], graph)

        self.assertNotIn(('example', '0001'), result['example'][0].dependencies)


class DisabledTestCase(SimpleTestCase):
    def test_nothing_is_spliced_without_a_graph(self):
        """
        Case: _detect_changes called without a migration graph, as makemigrations does for an initial run.
        Expected: The detected model changes are returned untouched, since there is nothing to compare against.
        """
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())
        changes = {'example': [build_migration('example', 'auto_1', [])]}

        with mock.patch.object(MigrationAutodetector, '_detect_changes', return_value=changes):
            result = autodetector._detect_changes(graph=None)

        self.assertEqual(result, changes)

    @override_settings(POSTGRES_OBJECTS={})
    def test_nothing_is_spliced_without_a_functions_module(self):
        """
        Case: The settings dict names no functions module.
        Expected: Stock Django behaviour, so a project that declares nothing is unaffected.
        """
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())
        changes = {'example': [build_migration('example', 'auto_1', [])]}

        with mock.patch.object(MigrationAutodetector, '_detect_changes', return_value=changes):
            result = autodetector._detect_changes(graph=MigrationGraph())

        self.assertEqual(result, changes)


class MigrationWriterTestCase(SimpleTestCase):
    def test_an_operation_round_trips_through_the_writer(self):
        """
        Case: Write a migration containing every operation and read the file back.
        Expected: The operations reconstruct to equal definitions, which is what makes a written migration reproduce the
                  object without referring back to the live declaration.
        """
        from django.db.migrations.writer import MigrationWriter

        previous = declare('Doubled').definition
        current = declare('Doubled', body='BEGIN RETURN input || input; END;').definition

        migration = build_migration(
            'example',
            '0001_objects',
            [
                AddFunction(current, hints={'target': 'ovens'}),
                AlterFunction(current, previous),
                RemoveFunction(previous),
            ],
        )

        source = MigrationWriter(migration).as_string()

        namespace = {}
        exec(compile(source, '<migration>', 'exec'), namespace)  # noqa: S102
        written = namespace['Migration']('0001_objects', 'example')

        self.assertEqual(
            [type(o).__name__ for o in written.operations], ['AddFunction', 'AlterFunction', 'RemoveFunction']
        )
        self.assertEqual(written.operations[0].definition, current)
        self.assertEqual(written.operations[0].hints, {'target': 'ovens'})
        self.assertEqual(written.operations[1].previous, previous)
        self.assertEqual(written.operations[2].definition, previous)
        self.assertEqual(written.operations[2].hints, {})

    def test_a_recalculation_round_trips_through_the_writer(self):
        """
        Case: Write a migration containing a RecalculateGeneratedField and read the file back.
        Expected: The operation reconstructs with the model, field and hints it was written with.
        """
        from django.db.migrations.writer import MigrationWriter

        migration = build_migration(
            'example',
            '0001_recalc',
            [RecalculateGeneratedField('GenModel', 'name_uppercased', hints={'target': 'ovens'})],
        )

        source = MigrationWriter(migration).as_string()

        namespace = {}
        exec(compile(source, '<migration>', 'exec'), namespace)  # noqa: S102
        written = namespace['Migration']('0001_recalc', 'example')

        operation = written.operations[0]
        self.assertEqual(type(operation).__name__, 'RecalculateGeneratedField')
        self.assertEqual((operation.model_name, operation.name), ('GenModel', 'name_uppercased'))
        self.assertEqual(operation.hints, {'target': 'ovens'})

    def test_a_refresh_round_trips_through_the_writer(self):
        """
        Case: Write a migration containing a RefreshMaterializedView and read the file back. This is the one operation
              projects write into migrations by hand, so nothing else exercises its serialization.
        Expected: The operation reconstructs with the definition and the CONCURRENTLY flag it was written with.
        """
        from django.db.migrations.writer import MigrationWriter

        definition = type(MaterializedView)(
            'Totals',
            (MaterializedView,),
            {'app_label': 'example', 'sql': 'SELECT id FROM example_cake', 'unique_index': ('id',)},
        ).definition

        migration = build_migration('example', '0002_refresh', [RefreshMaterializedView(definition, concurrently=True)])

        source = MigrationWriter(migration).as_string()

        namespace = {}
        exec(compile(source, '<migration>', 'exec'), namespace)  # noqa: S102
        written = namespace['Migration']('0002_refresh', 'example')

        operation = written.operations[0]
        self.assertEqual(type(operation).__name__, 'RefreshMaterializedView')
        self.assertEqual(operation.definition, definition)
        self.assertIs(operation.concurrently, True)
        self.assertEqual(operation.hints, {})


class RecordingMixin:
    """
    Stands in for another library's autodetection, layered the way django-pgtrigger layers its own.
    """

    def _detect_changes(self, convert_apps=None, graph=None):
        changes = super()._detect_changes(convert_apps, graph)
        changes.setdefault('recorded', [])

        return changes


class CompositionTestCase(SimpleTestCase):
    """
    Cover for how this package installs itself onto Django's migration commands, and for sharing them with another
    library that installs itself the same way.
    """

    def setUp(self):
        super().setUp()

        # Every slot is process-global, so each is put back the way it was found.
        for module in (makemigrations, migrate):
            self.addCleanup(setattr, module, 'MigrationAutodetector', module.MigrationAutodetector)
            self.addCleanup(setattr, module.Command, 'autodetector', module.Command.autodetector)

    def foreign(self):
        """
        An autodetector as another library leaves it: the stock one with a mixin of its own layered on.
        """
        return type('MigrationAutodetector', (RecordingMixin, MigrationAutodetector), {})

    def install_foreign(self):
        """
        Patch all four slots the way a library that patches from its AppConfig.ready does.
        """
        foreign = self.foreign()

        makemigrations.MigrationAutodetector = foreign
        migrate.MigrationAutodetector = foreign
        makemigrations.Command.autodetector = foreign
        migrate.Command.autodetector = foreign

        return foreign

    def test_the_stock_autodetector_composes_to_the_named_class(self):
        """
        Case: Compose onto Django's own autodetector, which is the ordinary case.
        Expected: The class this package declares, rather than an anonymous type, so what runs is importable and names
                  itself in a traceback and in the hint of Django's commands.E001.
        """
        self.assertIs(compose(MigrationAutodetector), DeclarativeObjectAutodetector)

    def test_an_autodetector_already_carrying_this_package_is_left_alone(self):
        """
        Case: Compose onto something that already has this package's mixin.
        Expected: Returned unchanged, which is what keeps installing twice from stacking two copies.
        """
        self.assertIs(compose(DeclarativeObjectAutodetector), DeclarativeObjectAutodetector)

    def test_another_librarys_autodetection_is_kept_and_this_packages_added(self):
        """
        Case: Compose onto an autodetector another library has already extended, then detect changes with it.
        Expected: Both contributions in the result: the other library's because it is layered underneath, and this
                  package's because it adds to whatever it is handed.
        """
        autodetector = compose(self.foreign())(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value={}),
            mock.patch(
                'postgres_objects.autodetector.detector.get_object_changes',
                return_value=({'example': [AddFunction(declare('Doubled').definition)]}, {}, {}),
            ),
        ):
            result = autodetector._detect_changes(graph=MigrationGraph())

        self.assertEqual(result['recorded'], [])
        self.assertEqual(result['example'][0].name, 'auto_db_objects')

    def test_every_slot_names_the_same_class(self):
        """
        Case: Install onto the migration commands.
        Expected: Both module-level names and both command attributes are the one composed class. The module-level names
                  matter because a library patching afterwards composes onto those rather than onto the command
                  attribute, so leaving them behind would have this package's contribution dropped without a word.
        """
        composed = patch_migrations()

        self.assertIs(makemigrations.MigrationAutodetector, composed)
        self.assertIs(migrate.MigrationAutodetector, composed)
        self.assertIs(makemigrations.Command.autodetector, composed)
        self.assertIs(migrate.Command.autodetector, composed)

    def test_installing_twice_installs_one_class(self):
        """
        Case: Install twice, as happens when an AppConfig is readied more than once.
        Expected: The same class both times, rather than one wrapped around the other.
        """
        self.assertIs(patch_migrations(), patch_migrations())

    def test_installing_after_another_library_keeps_both(self):
        """
        Case: Another library has already patched the commands when this package installs itself.
        Expected: One class carrying both, on every slot.
        """
        self.install_foreign()

        composed = patch_migrations()

        self.assertTrue(issubclass(composed, DeclarativeObjectAutodetectorMixin))
        self.assertTrue(issubclass(composed, RecordingMixin))
        self.assertIs(migrate.Command.autodetector, composed)

    def test_another_library_patching_afterwards_keeps_both(self):
        """
        Case: This package installs itself first and the other library patches on top, which is what the order of
              INSTALLED_APPS decides.
        Expected: Still one class carrying both. This is the case that fails when only the command attribute is written,
                  since the other library composes onto the module-level name it finds.
        """
        patch_migrations()

        composed = type('MigrationAutodetector', (RecordingMixin, makemigrations.MigrationAutodetector), {})

        self.assertTrue(issubclass(composed, DeclarativeObjectAutodetectorMixin))
        self.assertTrue(issubclass(composed, RecordingMixin))

    def test_the_installed_class_is_the_one_offered_to_a_project(self):
        """
        Case: Ask for the autodetector the way a project subclassing the commands has to.
        Expected: The installed class itself, since commands.E001 compares the two commands by identity and a fresh
                  composition each time would fail it.
        """
        composed = patch_migrations()

        self.assertIs(get_autodetector(), composed)
        self.assertIs(get_autodetector(), get_autodetector())

    def test_composing_the_same_base_twice_gives_one_class(self):
        """
        Case: Compose onto the same foreign autodetector from two places, as a project naming it in both of its commands
              does.
        Expected: The identical class. commands.E001 compares what the two commands name by identity, so an equal but
                  separate class would refuse to start.
        """
        foreign = self.foreign()

        self.assertIs(compose(foreign), compose(foreign))

    def test_a_commands_own_autodetector_is_kept_when_it_is_named(self):
        """
        Case: A library that ships its own commands and declares an autodetector on them rather than patching the
              module-level name, then a project subclassing that command the way the docs say.
        Expected: Both contributions. The library's class lives on its own Command, so it has to be passed in; the test
                  below covers what happens when it is not.
        """
        foreign = self.foreign()

        composed = get_autodetector(foreign)

        self.assertTrue(issubclass(composed, DeclarativeObjectAutodetectorMixin))
        self.assertTrue(issubclass(composed, RecordingMixin))

    def test_without_the_base_only_djangos_command_is_seen(self):
        """
        Case: The same subclass, but asking for the autodetector without naming what it is subclassing.
        Expected: This package's contribution only. An autodetector declared on another library's Command is not
                  reachable from Django's, which is the whole reason get_autodetector takes a base.
        """
        patch_migrations()
        foreign = self.foreign()

        self.assertFalse(issubclass(get_autodetector(), RecordingMixin))
        self.assertTrue(issubclass(get_autodetector(foreign), RecordingMixin))
