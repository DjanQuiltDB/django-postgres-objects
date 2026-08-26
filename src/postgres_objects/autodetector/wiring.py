"""
Composing the autodetection onto a base autodetector and installing it on Django's migration commands.
"""

from django.db.migrations.autodetector import MigrationAutodetector

from postgres_objects.autodetector.detector import DeclarativeObjectAutodetector, DeclarativeObjectAutodetectorMixin

#: The class built for each base composed so far. Composing is asked for from multiple places (both the ``migrate``
#: and ``makemigrations`` management commands) and Django's commands.E001 compares what each command names by identity.
_composed = {}


def compose(base):
    """
    This package's autodetection layered onto ``base``, or ``base`` itself if it already carries it.

    The named class is returned for the ordinary case, so what runs is importable and appears in tracebacks and in the
    hint of Django's commands.E001 under its own name rather than as an anonymous type. Stable across calls: composing
    the same base twice returns the one class, so a project naming it separately in its makemigrations and its migrate
    still has both commands agree.
    """
    if issubclass(base, DeclarativeObjectAutodetectorMixin):
        return base

    if base is MigrationAutodetector:
        return DeclarativeObjectAutodetector

    if base not in _composed:
        _composed[base] = type(
            DeclarativeObjectAutodetector.__name__,
            (DeclarativeObjectAutodetectorMixin, base),
            {'__module__': DeclarativeObjectAutodetector.__module__},
        )

    return _composed[base]


def patch_migrations():
    """
    Layer this package's autodetection onto Django's migration commands.

    Four slots are written, not two. A library that patches after this one reads the module-level name rather than the
    command attribute (for example, django-pgtrigger composes onto makemigrations.MigrationAutodetector and then assigns
    Command.autodetector from it) so writing only the command attribute would cause this package's contribution to be
    silently dropped whenever that library's app is listed later.
    """
    from django.core.management.commands import makemigrations, migrate

    composed = compose(makemigrations.MigrationAutodetector)

    makemigrations.MigrationAutodetector = composed
    migrate.MigrationAutodetector = composed
    makemigrations.Command.autodetector = composed
    migrate.Command.autodetector = composed

    return composed


def get_autodetector(base=None):
    """
    The autodetector to name on a migration command a project subclasses itself.

    Without an argument, this composes onto what Django's own command carries, which is the right answer when the
    command being subclassed is Django's. A command from another library may declare an autodetector of its own instead,
    and that one is invisible from here, so pass it as ``base`` to keep it:

        class Command(TheirMakeMigrations):
            autodetector = get_autodetector(TheirMakeMigrations.autodetector)

    This should be done in both ``migrate`` and ``makemigrations`` command overrides.
    """
    if base is None:
        from django.core.management.commands import makemigrations  # noqa: PLC0415

        base = makemigrations.Command.autodetector

    return compose(base)
