from unittest import mock

from django.core.management.commands import makemigrations, migrate
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase, override_settings

from postgres_objects import Function, View
from postgres_objects.autodetector import (
    DeclarativeObjectAutodetector,
    DeclarativeObjectAutodetectorMixin,
    build_migration,
    compose,
    get_autodetector,
    get_migrated_objects,
    get_object_changes,
    get_ordered_nodes,
    patch_migrations,
)
from postgres_objects.operations import AddFunction, AddView, AlterFunction, RemoveFunction, RemoveView

MODULE_PATH = 'db_functions'


def declare(class_name, **attrs):
    namespace = {
        'app_label': 'example',
        'arguments': 'input TEXT',
        'returns': 'TEXT',
        'body': 'BEGIN RETURN input; END;',
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

        self.assertEqual(get_migrated_objects(graph), {('example', 'function', 'doubled'): (definition, {})})

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

        self.assertEqual(get_migrated_objects(build_graph(nodes)), {('example', 'function', 'doubled'): (second, {})})

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
            get_migrated_objects(graph), {('example', 'function', 'doubled'): (definition, {'target': 'ovens'})}
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


class GetObjectChangesTestCase(SimpleTestCase):
    def changes(self, declared, graph):
        with mock.patch(
            'postgres_objects.autodetector.changes.get_declarations',
            return_value={(d.resolved_app_label, d.definition.kind, d.name): d for d in declared},
        ):
            return get_object_changes(graph)

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

    def test_changed_hints_add_first_and_remove_after(self):
        """
        Case: The declaration is annotated to live somewhere else.
        Expected: Same shape as a signature change, because the new copy is created somewhere the old one is not.
        """
        previous = declare('Doubled').definition
        declaration = declare('Doubled', router_hints={'target': 'ovens'})
        graph = build_graph(migration_with('example', '0001', AddFunction(previous)))

        leading, trailing = self.changes([declaration], graph)

        self.assertEqual(leading['example'][0].hints, {'target': 'ovens'})
        self.assertEqual(trailing['example'][0].hints, {})

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
            return get_object_changes(graph)

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

    def test_an_unchanged_view_produces_nothing(self):
        """
        Case: A view matching what the migrations already created.
        Expected: No operations, so a project that changed nothing writes no migration.
        """
        declaration = declare_view('Uppercased')
        graph = build_graph(migration_with('example', '0001', AddView(declaration.definition)))

        self.assertEqual(self.changes([declaration], graph), ({}, {}))

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


class SplicingTestCase(SimpleTestCase):
    def detect(self, leading, trailing, changes):
        autodetector = DeclarativeObjectAutodetector(ProjectState(), ProjectState())

        with (
            mock.patch.object(MigrationAutodetector, '_detect_changes', return_value=changes),
            mock.patch('postgres_objects.autodetector.detector.get_object_changes', return_value=(leading, trailing)),
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
                return_value=({'example': [AddFunction(declare('Doubled').definition)]}, {}),
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
