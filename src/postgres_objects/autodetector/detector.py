"""
The autodetector mixin that splices the object migrations into whatever Django detected for the models.
"""

from django.db.migrations import Migration
from django.db.migrations.autodetector import MigrationAutodetector

from postgres_objects.autodetector.changes import get_object_changes
from postgres_objects.registry import get_functions_module


def build_migration(app_label, name, operations):
    # Migration.__init__ copies the class-level operations/dependencies into fresh per-instance lists, so a plain
    # instance is safely mutable.
    migration = Migration(name, app_label)
    migration.operations = operations

    return migration


class DeclarativeObjectAutodetectorMixin:
    """
    Adds postgres-object migrations to whatever Django detected for the models.

    The splicing happens inside _detect_changes, before changes() hands the result to arrange_for_graph (which is what
    numbers the migrations, names them and rewrites every dependency through its name map).

    This is a mixin rather than a subclass of MigrationAutodetector, because we want to coexist with another library
    which may want to detect something of its own in the same run rather than overwrite it by creating a subclass that
    doesn't inherit the changes for that other library. Django's changes are asked for first and only added to
    afterwards, so this approach layers correctly on either side of another such mixin. (This can only coexist with
    other libraries using a similar Mixin approach; for libraries that don't, we document a workaround in our
    installation instructions.)
    """

    def _detect_changes(self, convert_apps=None, graph=None):
        changes = super()._detect_changes(convert_apps, graph)

        # The setting names the module to read declarations from. This functionality is disabled by leaving it unset.
        module_path = get_functions_module()
        if graph is None or not module_path:
            return changes

        leading, trailing = get_object_changes(graph, module_path)

        # Each app's first migration as Django detected it, before anything is spliced in front of it.
        first_model_migration = {
            app_label: app_migrations[0] for app_label, app_migrations in changes.items() if app_migrations
        }

        object_migrations = {}
        for app_label in sorted(leading):
            migration = build_migration(app_label, 'auto_db_objects', leading[app_label])
            app_migrations = changes.setdefault(app_label, [])
            if app_migrations:
                # Whatever ran first now depends on the objects being in place, so a model migration adding a generated
                # column can count on the function its expression calls.
                app_migrations[0].dependencies.append((app_label, migration.name))
            app_migrations.insert(0, migration)
            object_migrations[app_label] = migration

        # A model in one app may use an object declared in another, so every app's first model migration also depends on
        # the other apps' object migrations.
        for app_label, model_migration in first_model_migration.items():
            for object_app, object_migration in object_migrations.items():
                if object_app != app_label:
                    model_migration.dependencies.append((object_app, object_migration.name))

        # Each app's last migration so far — none of these are removal migrations yet.
        last_regular_migration = {
            app_label: app_migrations[-1] for app_label, app_migrations in changes.items() if app_migrations
        }

        # Removals mirror the additions: any app's migrations may have been the last thing referencing an object, so
        # each removal migration waits for every app's last regular migration.
        for app_label in sorted(trailing):
            migration = build_migration(app_label, 'auto_db_objects_removed', trailing[app_label])
            for other_app, last_migration in last_regular_migration.items():
                migration.dependencies.append((other_app, last_migration.name))
            changes.setdefault(app_label, []).append(migration)

        return changes


class DeclarativeObjectAutodetector(DeclarativeObjectAutodetectorMixin, MigrationAutodetector):
    """
    The composition over stock Django, and what runs when nothing else is layered in.
    """

    pass
