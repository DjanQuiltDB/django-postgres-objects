========
Overview
========

Django's migration framework knows about tables, columns, indexes and constraints. It knows nothing about the other
things that live in a PostgreSQL schema, so a function is normally created with a hand-written ``RunSQL``.

That works. What it does not do is notice when the two drift apart. Nothing compares the ``CREATE OR REPLACE FUNCTION``
in your migration against what you meant it to be, editing the function is a step you have to remember to take by hand,
and ``makemigrations --check`` will never tell you that you forgot.

This package lets you declare such an object once, as a class, and have ``makemigrations`` write the operations.


Declarations and definitions
----------------------------

A managed object is two things:

A **declaration** is the class you write. It lives in an app's declarations module the same way a model lives in
``models.py``, it carries the attributes that describe the object, and it is what your project annotates and refers to.

A **definition** is a frozen value object holding exactly the attributes that end up in SQL. Operations hold one, and it
is what gets written into a migration file.

This mirrors Django's own split between a model class and the ``ModelState`` the migration framework works with, and it
buys the same thing: a migration records the object as it was when the migration was written. Editing a declaration
afterwards produces a *new* migration rather than silently changing the meaning of an old one.


What it does not do
-------------------

It does not read the database. Declarations are compared against the operations already present in your migration graph,
never against a live schema. Nothing has to be reachable for ``makemigrations`` to work out what changed, which is what
keeps ``--check`` usable in CI.

It also does not repair drift it did not cause. If somebody changes a function by hand in a database, this package will
not notice and will not correct it; the next migration will simply replace it.

Essentially, these are the same limitations that you encounter with regular model migrations in Django, but the risk is
more substantial with functions or views since these are traditionally managed outside Django, whereas model tables
traditionally are not.


Current scope
-------------

Functions and views, including materialized views. Each kind is declared in its own module, named by its own settings
key, so a project can manage one and not the other.

The managed kinds are fixed at these two: the table of kinds the autodetector works from is not extensible.
``DeclarativeObject`` and ``ObjectDefinition`` are exported with a future registration API in mind, but subclassing them
to add a third-party kind of object is unsupported today, as nothing would pick the new kind up.

Triggers (another kind of PostgreSQL object not natively managed by Django) are intentionally out of scope:
`django-pgtrigger <https://github.com/AmbitionEng/django-pgtrigger>`_ is already a mature answer for those objects and
projects can safely use that library alongside this one.
