"""
Scaffolding for running makemigrations in a test and reading back what it wrote.

The migrations go to a throwaway package on sys.path rather than into the app being migrated, so a run leaves nothing
behind and each test starts from no migrations at all. Every app of the test project gets one, not only the app being
migrated: makemigrations keeps the migrations of any app the named one depends on, and those would otherwise be written
into the real tree.
"""

import os
import shutil
import sys
import tempfile

from django.apps import apps
from django.core.management import call_command
from django.test import override_settings

MIGRATIONS_PACKAGE = 'tmp_test_migrations'


def project_app_labels():
    """
    The apps of the test project, which are the ones makemigrations could write for. Django's own are left out; they
    ship their migrations and never have changes to detect.
    """
    return [config.label for config in apps.get_app_configs() if not config.name.startswith('django.')]


class MigrationWritingMixin:
    #: The app to run makemigrations for, and the one read() reads from unless told otherwise.
    app_label = None

    #: Further apps to name in the same run, for a change that spans them.
    also_migrate = ()

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

        self.packages = {}
        self.modules = {}

        for app_label in project_app_labels():
            module = '{}_{}'.format(MIGRATIONS_PACKAGE, app_label)
            package = os.path.join(self.directory, module)
            os.mkdir(package)
            with open(os.path.join(package, '__init__.py'), 'w'):
                pass

            self.packages[app_label] = package
            self.modules[app_label] = module
            self.addCleanup(sys.modules.pop, module, None)

        sys.path.insert(0, self.directory)
        self.addCleanup(sys.path.remove, self.directory)

        self.package = self.packages[self.app_label]

    def make_migrations(self, app_label=None):
        with override_settings(MIGRATION_MODULES=self.modules):
            call_command('makemigrations', self.app_label, *self.also_migrate, verbosity=0)

        return self.written(app_label)

    def written(self, app_label=None):
        package = self.packages[app_label or self.app_label]

        return sorted(name for name in os.listdir(package) if name.endswith('.py') and name != '__init__.py')

    def read(self, name, app_label=None):
        with open(os.path.join(self.packages[app_label or self.app_label], name)) as handle:
            return handle.read()
