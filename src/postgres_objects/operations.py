"""
Migration operations for declared database objects.
"""

from django.db import router
from django.db.migrations.operations.base import Operation

from postgres_objects.base import DeclarativeObject


class DatabaseObjectOperation(Operation):
    reduces_to_sql = True
    reversible = True
    atomic = True

    #: How the autodetector's fold over the migration graph should read this operation. An operation that is neither
    #: (a refresh, say) does not change what the migrations say exists, so the fold ignores it.
    creates = False
    removes = False

    #: The verb this operation is described by. The noun comes from the definition.
    verb = None

    def __init__(self, definition, hints=None):
        self.definition = definition

        # Where the object belongs is not a property of the object itself, it is a property of the project managing it.
        # The autodetector copies the declaration's router_hints onto the operation, so the migration records the
        # placement that was in force when it was written rather than looking it up again at apply time.
        self.hints = dict(hints or {})

    def deconstruct(self):
        kwargs = {'definition': self.definition}
        if self.hints:
            kwargs['hints'] = self.hints
        return (self.__class__.__qualname__, [], kwargs)

    def state_forwards(self, app_label, state):
        """
        A database object of this kind has no representation in Django's migration state, so there is nothing to record.
        What has been created is reconstructed from the migration graph instead, by the autodetector.
        """
        pass

    def allowed(self, app_label, schema_editor):
        # Migrations written before a placement plugin was installed carry no hints. At apply time the base-class
        # default is the placement an unannotated declaration would get, so falling back to it lets those pre-plugin
        # migrations apply instead of hard-failing in the router. In a plain project both are empty and nothing changes.
        hints = self.hints or DeclarativeObject.router_hints

        return router.allow_migrate(schema_editor.connection.alias, app_label, **hints)

    def target_schema(self, schema_editor):
        """
        The schema this connection creates objects in, which is where a DROP has to be aimed.

        An unqualified DROP resolves through the whole search path, so on a connection whose path covers more than its
        own schema it can reach past the copy it was meant to remove and drop somebody else's. current_schema() is the
        first existing schema on the path, which is exactly where an unqualified CREATE just put the object.

        Override this to name the schema outright and save the round trip.
        """
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('SELECT current_schema()')
            return cursor.fetchone()[0]

    def describe(self):
        return '{} {} {}'.format(self.verb, self.definition.object_noun, self.definition.description)

    @property
    def migration_name_fragment(self):
        return '{}_{}_{}'.format(self.verb.lower(), self.definition.object_noun, self.definition.name.lower())

    def _execute(self, schema_editor, statements):
        for statement in statements:
            # params=None so a literal % in a body or a view's SQL is not read as a placeholder.
            schema_editor.execute(statement, params=None)

    def _create(self, app_label, schema_editor, definition=None):
        if self.allowed(app_label, schema_editor):
            self._execute(schema_editor, (definition or self.definition).create_statements())

    def _drop(self, app_label, schema_editor, definition=None):
        if self.allowed(app_label, schema_editor):
            definition = definition or self.definition
            self._execute(schema_editor, definition.drop_statements(self.target_schema(schema_editor)))


class AddOperation(DatabaseObjectOperation):
    """
    Create the object, and drop it again when reversed.
    """

    creates = True
    verb = 'Create'

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._drop(app_label, schema_editor)


class AlterOperation(DatabaseObjectOperation):
    """
    Replace the object in place, carrying the previous definition so the change can be reversed.

    Only for objects whose CREATE can replace an existing one; a materialized view, for instance, has no such form and
    is changed by dropping and recreating instead.
    """

    creates = True
    verb = 'Alter'

    def __init__(self, definition, previous, hints=None):
        super().__init__(definition, hints=hints)
        self.previous = previous

    def deconstruct(self):
        name, args, kwargs = super().deconstruct()
        kwargs['previous'] = self.previous
        return (name, args, kwargs)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor, definition=self.previous)


class RemoveOperation(DatabaseObjectOperation):
    """
    Drop the object, and create it again when reversed.
    """

    removes = True
    verb = 'Remove'

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._drop(app_label, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor)


class AddFunction(AddOperation):
    pass


class AlterFunction(AlterOperation):
    pass


class RemoveFunction(RemoveOperation):
    pass


class AddView(AddOperation):
    pass


class RemoveView(RemoveOperation):
    pass


class RefreshMaterializedView(DatabaseObjectOperation):
    """
    Repopulate a materialized view as a migration runs.

    This is where the *first* fill of a view declared ``with_data=False`` belongs. PostgreSQL refuses to read such a
    view until a refresh has run, and the migration that created it is the only thing that reaches every database those
    migrations are applied to. Written by hand, since nothing in the declaration says the fill is wanted.

    A refresh for staleness is another job entirely: when stored rows fall behind the tables underneath them (after an
    import, on a schedule, at the end of a task) is not something a migration can be written for.
    :meth:`~postgres_objects.views.MaterializedView.refresh` should be used in that case.

    The operation is never written by the autodetector, since whether a view's contents are stale is not something a
    declaration can say. It creates and removes nothing, so the autodetector's fold over the graph ignores it. It is the
    responsibility of the project developer creating the MaterializedView to manage this.

    CONCURRENTLY keeps the view readable while it refreshes, which PostgreSQL only allows when the view carries a unique
    index and has been populated at least once. Only the unique-index half is checked here: whether the view has been
    filled is runtime state no operation can know, and the ``with_data`` field would wrongly refuse every concurrent
    refresh after the first fill. The first fill of a ``with_data=False`` view must therefore be a plain refresh, as
    PostgreSQL refuses a concurrent one against a never-populated view.
    """

    verb = 'Refresh'

    def __init__(self, definition, concurrently=False, hints=None):
        super().__init__(definition, hints=hints)

        if not getattr(definition, 'materialized', False):
            raise ValueError('{} is not a materialized view, so it cannot be refreshed.'.format(definition.db_name))

        # Checked as the operation is built rather than as it runs, so a migration that could never work fails while it
        # is being written. The definition holds the rule, which is what keeps this and a refresh from code saying the
        # same thing.
        definition.check_refresh(concurrently)

        self.concurrently = concurrently

    def deconstruct(self):
        name, args, kwargs = super().deconstruct()
        if self.concurrently:
            kwargs['concurrently'] = True
        return (name, args, kwargs)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if self.allowed(app_label, schema_editor):
            self._execute(schema_editor, (self.definition.refresh_sql(concurrently=self.concurrently),))

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        """
        Nothing. A refresh has no inverse, and rolling a migration back must not fail on that account.
        """
        pass


class RecalculateGeneratedField(Operation):
    """
    Rewrite a stored generated column so every row holds what its **current** expression computes.

    PostgreSQL computes a stored generated column when a row is written, not when a function behind it changes, so
    replacing a function's body leaves existing rows holding values the old body produced. SET EXPRESSION, handed the
    expression the column already has, forces the table rewrite that brings them up to date.

    Written by the autodetector when a :class:`postgres_objects.functions.Function` declaration used by a
    :class:`postgres_objects.GeneratedField` changes what it computes.

    The operation is not a DatabaseObjectOperation: it belongs to a model, not to a declared object, and the
    autodetector's fold over the migration graph ignores it since it changes no definition.
    """

    reduces_to_sql = True
    reversible = True
    atomic = True

    def __init__(self, model_name, name, hints=None):
        self.model_name = model_name
        self.name = name
        self.hints = dict(hints or {})

    def deconstruct(self):
        kwargs = {'model_name': self.model_name, 'name': self.name}
        if self.hints:
            kwargs['hints'] = self.hints
        return (self.__class__.__qualname__, [], kwargs)

    def state_forwards(self, app_label, state):
        """
        Nothing changes in state: the column keeps the definition it had, only its stored values move.
        """
        pass

    def allowed(self, app_label, schema_editor):
        # The rewritten table is the model's, so the model is named alongside any explicit hints and a router can
        # resolve placement from either.
        return router.allow_migrate(
            schema_editor.connection.alias, app_label, model_name=self.model_name.lower(), **self.hints
        )

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if not self.allowed(app_label, schema_editor):
            return

        model = to_state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.name)

        if not getattr(field, 'generated', False) or not field.db_persist:
            # A virtual column is computed on read, so it can never go stale.
            return

        expression_sql, params = field.generated_sql(schema_editor.connection)

        # The table is named unqualified, like everything else here, so the statement lands wherever the connection's
        # search path points. Unlike a hand-written body, the compiled expression uses %s placeholders, so the params
        # are passed along rather than suppressed.
        schema_editor.execute(
            'ALTER TABLE {table} ALTER COLUMN {column} SET EXPRESSION AS ({expression})'.format(
                table=schema_editor.quote_name(model._meta.db_table),
                column=schema_editor.quote_name(field.column),
                expression=expression_sql,
            ),
            params,
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        """
        Nothing. The reverse plan restores the old function body only after this step runs, so a recompute here would
        rewrite the table against the very body being reversed away. Unapplying a body change therefore leaves values
        the newer body computed; when that matters, the user should recalculate by hand with this operation in a later
        migration.
        """
        pass

    def describe(self):
        return 'Recalculate generated field {} on {}'.format(self.name, self.model_name)

    @property
    def migration_name_fragment(self):
        return 'recalculate_{}_{}'.format(self.model_name.lower(), self.name.lower())
