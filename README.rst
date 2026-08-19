=======================
django-postgres-objects
=======================

PostgreSQL objects (i.e. non-tables) have no representation in Django's migration state, so the usual way to manage
these in Django migrations is through hand-written ``RunSQL``. That works, but nothing notices when the declaration and
the database drift apart, and ``makemigrations --check`` will never tell you.

This package lets you declare such an object as a class, the same way a model is a class, have ``makemigrations`` write
the operations for you, and have ``migrate`` perform the operations for you.

.. code-block:: python

    # example/db_functions.py
    from postgres_objects import Function


    class AllUppercase(Function):
        arguments = 'input TEXT'
        returns = 'TEXT'
        volatility = 'IMMUTABLE'
        strict = True
        parallel = 'SAFE'
        body = """
            BEGIN
                RETURN UPPER(input);
            END;
        """

Views are declared the same way, in a module of their own::

    # example/db_views.py
    from postgres_objects import View


    class UppercasedCakes(View):
        sql = 'SELECT id, name_uppercased FROM example_cake'

A view masking an existing model (e.g. a subset of its columns, a row condition or an aggregate) can declare the
queryset instead of the SQL, and then its columns are declared exactly once: the compiled SELECT goes into the
migration, and the declaration exposes ``.objects``, a generated unmanaged model for reading the view back::

    from django.db.models import Count

    from example.models import Cake
    from postgres_objects import MaterializedView


    class CakeCounts(MaterializedView):
        unique_index = ('name',)

        def queryset():
            return Cake.objects.values('name').annotate(cakes=Count('id'))

    CakeCounts.objects.filter(cakes__gt=3)

A materialized view stores its rows, and no declaration can say when they have gone stale, so repopulating one is a call
you make from wherever you know the data has moved::

    CakeCounts.refresh()

Point a setting at the module each kind lives in, relative to each app::

    POSTGRES_OBJECTS = {
        'FUNCTIONS_MODULE_PATH': 'db_functions',
        'VIEWS_MODULE_PATH': 'db_views',
    }

``manage.py makemigrations`` now writes the operations for whatever you added, changed or removed. Function migrations
are written *before* that app's model migrations and removals *after* them, so a model migration adding a generated
column can rely on the function its expression calls, and a function is only dropped once nothing refers to it any more.
Views are placed the other way round, since a view reads from tables rather than being read by them.

The declaration is callable, so the same class serves the migration that creates the function and the queries that call
it:

.. code-block:: python

    from django.db.models import F

    from example.db_functions import AllUppercase

    Cake.objects.annotate(uppercased=AllUppercase(F('name')))

    # or as a generated column
    from postgres_objects import GeneratedField


    class Cake(models.Model):
        name = models.CharField(max_length=128)
        name_uppercased = GeneratedField(
            expression=AllUppercase(F('name')),
            output_field=models.TextField(),
            db_persist=True,
        )

``postgres_objects.GeneratedField`` is Django's ``GeneratedField`` plus one promise: when a declared function the column
depends on changes what it computes, the migration that alters the function is followed by one that recalculates the
stored values, which PostgreSQL does not do on its own. Django's plain ``GeneratedField`` works too, but its stored
values are then left as the old body computed them.

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

* Python 3.14
* Django 6.0
* PostgreSQL 17 or 18
