"""
Migration operations for declared database objects.
"""

from django.db import router
from django.db.migrations.operations.base import Operation


class DatabaseObjectOperation(Operation):
    reduces_to_sql = True
    reversible = True
    atomic = True

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
        return router.allow_migrate(schema_editor.connection.alias, app_label, **self.hints)

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

    def _create(self, app_label, schema_editor, definition=None):
        if self.allowed(app_label, schema_editor):
            schema_editor.execute((definition or self.definition).create_sql(), params=None)

    def _drop(self, app_label, schema_editor, definition=None):
        if self.allowed(app_label, schema_editor):
            definition = definition or self.definition
            schema_editor.execute(definition.drop_sql(self.target_schema(schema_editor)), params=None)


class AddFunction(DatabaseObjectOperation):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._drop(app_label, schema_editor)

    def describe(self):
        return 'Create function {}'.format(self.definition.signature)

    @property
    def migration_name_fragment(self):
        return 'create_function_{}'.format(self.definition.name.lower())


class AlterFunction(DatabaseObjectOperation):
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

    def describe(self):
        return 'Alter function {}'.format(self.definition.signature)

    @property
    def migration_name_fragment(self):
        return 'alter_function_{}'.format(self.definition.name.lower())


class RemoveFunction(DatabaseObjectOperation):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._drop(app_label, schema_editor)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._create(app_label, schema_editor)

    def describe(self):
        return 'Remove function {}'.format(self.definition.signature)

    @property
    def migration_name_fragment(self):
        return 'remove_function_{}'.format(self.definition.name.lower())
