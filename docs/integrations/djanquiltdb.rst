===========
DjanQuiltDB
===========

This library was written in conjunction with `DjanQuiltDB <https://github.com/djanquiltdb/djanquiltdb>`_, which shards a
Django project across PostgreSQL schemas and nodes. Both libraries work on their own: using django-postgres-objects
does not need DjanQuiltDB, or any familiarity with it.

In a sharded project, each declared function and view also has to be placed in the right schemas. DjanQuiltDB's
``postgres-objects`` extra installs a plugin that provides decorators for this, analogous to its model decorators. The
plugin also runs the recalculation of a generated column in each schema that has the column::

    pip install djanquiltdb[postgres-objects]

See the `plugin's documentation <https://djanquiltdb.readthedocs.io/en/latest/plugins/postgres-objects/index.html>`_
for how to use it.
