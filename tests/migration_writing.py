"""
Scaffolding for running makemigrations in a test and reading back what it wrote.

The migrations go to a throwaway package on sys.path rather than into the app being migrated, so a run leaves nothing
behind and each test starts from no migrations at all.
"""

import os
import shutil
import sys
import tempfile

from django.core.management import call_command
from django.test import override_settings

MIGRATIONS_PACKAGE = 'tmp_test_migrations'


class MigrationWritingMixin:
    #: The app to run makemigrations for.
    app_label = None

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

        package = os.path.join(self.directory, MIGRATIONS_PACKAGE)
        os.mkdir(package)
        with open(os.path.join(package, '__init__.py'), 'w'):
            pass

        sys.path.insert(0, self.directory)
        self.addCleanup(sys.path.remove, self.directory)
        self.addCleanup(sys.modules.pop, MIGRATIONS_PACKAGE, None)

        self.package = package

    def make_migrations(self):
        with override_settings(MIGRATION_MODULES={self.app_label: MIGRATIONS_PACKAGE}):
            call_command('makemigrations', self.app_label, verbosity=0)

        return sorted(name for name in os.listdir(self.package) if name.endswith('.py') and name != '__init__.py')

    def read(self, name):
        with open(os.path.join(self.package, name)) as handle:
            return handle.read()
