=======================
django-postgres-objects
=======================

PostgreSQL objects (i.e. non-tables) have no representation in Django's migration state, so the usual way to manage
these in Django migrations is through hand-written``RunSQL``. That works, but nothing notices when the declaration and
the database drift apart, and ``makemigrations --check`` will never tell you.

This package lets you declare such an object as a class, the same way a model is a class, have ``makemigrations`` write
the operations for you, and have ``migrate`` perform the operations for you.

Relationship to DjanQuiltDB
---------------------------

While this library was written in conjunction with `DjanQuiltDB <https://github.com/djanquiltdb/djanquiltdb>`_ and the
libraries were designed to work together seamlessly in projects where both are installed, both libraries are written to
be used as standalone libraries as well. For using django-postgres-objects, it is not necessary to use or be familiar
with DjanQuiltDB.

If you wish to use DjanQuiltDB features for objects managed through django-postgres-objects, you can install the
``djanquiltdb[postgres-objects]`` extra. For more information, please refer to the DjanQuiltDB documentation.

Scope limitations
-----------------

A major type of PostgreSQL object that this library is intentionally not covering is triggers, since there is already a
very mature library available for this in `django-pgtrigger <https://github.com/AmbitionEng/django-pgtrigger>`_.

Requirements
------------

Python 3.14+, Django 6.0, PostgreSQL.
