"""
The autodetector mixin that splices the object migrations into whatever Django detected for the models.
"""

import warnings

from django.db.migrations import Migration
from django.db.migrations.autodetector import MigrationAutodetector

from postgres_objects.autodetector.changes import KINDS, get_migrated_objects, get_object_changes
from postgres_objects.autodetector.ordering import (
    get_create_dependencies,
    get_dependency_targets,
    get_drop_dependencies,
    refuse_cyclic_apps,
)


class UnmigratedAppWarning(RuntimeWarning):
    """
    Database-object changes were detected for an app makemigrations will not write migrations for.
    """

    pass


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

        # A settings key names the module each kind of declaration is read from. Naming none of them disables all.
        if graph is None or not any(get_module() for _, _, get_module in KINDS):
            return changes

        leading, trailing, creators = get_object_changes(graph, self.from_state, self.to_state)

        # arrange_for_graph deletes every change of an app it will not write an initial migration for and rewrites
        # other apps' dependencies on them to (app, '__first__'), which the loader silently discards for unmigrated
        # apps: makemigrations reports success, writes nothing, and the ordering is gone. The questioner's answer is
        # Django's own decision, verbatim: an app named on the command line or holding a migrations package passes.
        # A warning rather than an error, because migrate runs this same detection for its has-changes notice, and an
        # error here would block every migrate of an affected project.
        for app_label in sorted(set(leading) | set(trailing)):
            if not graph.leaf_nodes(app_label) and not self.questioner.ask_initial(app_label):
                warnings.warn(
                    "The app '{0}' has database-object changes, but makemigrations will not write an initial "
                    'migration for it, so its changes are silently dropped along with the ordering between it and '
                    "other apps' migrations. Create {0}/migrations/__init__.py, or name the app on the "
                    'makemigrations command line.'.format(app_label),
                    UnmigratedAppWarning,
                    stacklevel=2,
                )

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

            # Reworking an object another app's migration created only orders correctly with a dependency on that
            # creator. Its node names an on-disk migration, which arrange_for_graph's renaming never touches.
            migration.dependencies.extend(sorted(creators.get(app_label, ())))

        # A model in one app may use an object declared in another, so every app's first model migration also depends on
        # the other apps' object migrations.
        for app_label, model_migration in first_model_migration.items():
            for object_app, object_migration in object_migrations.items():
                if object_app != app_label:
                    model_migration.dependencies.append((object_app, object_migration.name))

        # Dropping a view another app's view reads from means that that app's drops need to have run already.
        drops = get_drop_dependencies(leading)
        refuse_cyclic_apps(drops, 'The views dropped by')
        for app_label, other_apps in drops.items():
            for other_app in sorted(other_apps):
                object_migrations[app_label].dependencies.append((other_app, object_migrations[other_app].name))

        # Each app's last migration so far, excluding removal migrations.
        last_regular_migration = {
            app_label: app_migrations[-1] for app_label, app_migrations in changes.items() if app_migrations
        }

        # Every trailing migration is built before any is wired, because a view in one app may read a view another app
        # creates in the very same run.
        trailing_migrations = {
            app_label: build_migration(app_label, 'auto_db_objects_last', trailing[app_label])
            for app_label in sorted(trailing)
        }
        # Folding the graph a second time, so that get_object_changes does not have to also return every migrated
        # object. The graph is already in memory and this runs once per makemigrations.
        creates = get_create_dependencies(
            trailing,
            get_dependency_targets(
                trailing, trailing_migrations, get_migrated_objects(graph), graph, set(last_regular_migration)
            ),
        )
        # An edge only exists where the dependency IS the other app's trailing migration. A reference can also resolve
        # to an on-disk migration of an app that happens to have a trailing migration of its own, and that is no edge
        # between the trailing pair. Before arrange_for_graph renames anything the trailing migrations carry the
        # sentinel name, which no on-disk migration can be called, so comparing the full node tells the two apart.
        refuse_cyclic_apps(
            {
                app_label: {
                    node[0]
                    for node in nodes
                    if node[0] in trailing_migrations and node[1] == trailing_migrations[node[0]].name
                }
                for app_label, nodes in creates.items()
            },
            'The views created by',
        )

        # Removals mirror the additions: any app's migrations may have been the last thing referencing an object, so
        # each removal migration waits for every app's last regular migration.
        for app_label, migration in trailing_migrations.items():
            for other_app, last_migration in last_regular_migration.items():
                migration.dependencies.append((other_app, last_migration.name))

            migration.dependencies.extend(creates[app_label])
            changes.setdefault(app_label, []).append(migration)

        return changes


class DeclarativeObjectAutodetector(DeclarativeObjectAutodetectorMixin, MigrationAutodetector):
    """
    The composition over stock Django, and what runs when nothing else is layered in.
    """

    pass
