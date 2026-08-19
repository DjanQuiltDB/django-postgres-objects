=======
Queries
=======

A declaration is callable, and calling it returns a ``django.db.models.Func`` you can use anywhere an expression is
accepted. The same class therefore serves both the migration that creates the function and the queries that call it:

.. code-block:: python

    from django.db.models import F

    from example.db_functions import AllUppercase

    Cake.objects.annotate(uppercased=AllUppercase(F('name'))).filter(uppercased='CHOCOLATE CAKE')

Declare ``output_field`` if you want annotations to know the result type without repeating it:

.. code-block:: python

    from django.db import models


    class AllUppercase(Function):
        output_field = models.TextField()
        ...

It can still be given per call, as with any Django expression:

.. code-block:: python

    AllUppercase(F('name'), output_field=models.CharField())

The function is always named unqualified in the generated SQL. That is deliberate: the function may live in a different
schema from the one the connection is pointed at while still being on its search path, and qualifying the call would pin
it to whichever schema happened to build the query.

Because calling the class builds an expression, a declaration is never instantiated. Doing so by accident raises
``TypeError`` rather than quietly handing you something that is not what you meant.


Querying declared views
-----------------------

A view declared as a queryset serves its reads the same way: the declaration exposes ``.objects``, the manager of a
model generated from the queryset's columns:

.. code-block:: python

    from example.db_views import CakeCounts

    CakeCounts.objects.filter(cakes__gt=3).order_by('-cakes')

That model is deliberately not part of any installed app, so ``makemigrations`` never mistakes the view for a table to
create. See :doc:`views` for how the model is built, how its key is chosen, and when to write an unmanaged model by hand
instead.

The manager is an ordinary one, so a queryset over it is also a body another view can be declared as, which is how a
view is built on top of a view, and how the dependency between the two comes to be known. See :doc:`views` again.

A materialized view is read the same way, but what it holds is only as fresh as its last refresh, and asking for one is
a runtime call like any other:

.. code-block:: python

    CakeCounts.refresh(concurrently=True)

Both body flavors have it, raw ``sql`` included, since repopulating a view needs nothing but its name. See
:doc:`views` for what ``concurrently`` requires and which connection the refresh runs on.


Generated columns
-----------------

A stored generated column's expression normally calls a function, and that function has to exist before the column can
be created. This is the case the migration ordering exists for:

.. code-block:: python

    # example/models.py
    from django.db import models
    from django.db.models import F

    from example.db_functions import AllUppercase
    from postgres_objects import GeneratedField


    class Cake(models.Model):
        name = models.CharField('name', max_length=128)
        name_uppercased = GeneratedField(
            expression=AllUppercase(F('name')),
            output_field=models.TextField(),
            db_persist=True,
        )

``makemigrations`` writes the function migration first and makes the model migration depend on it.

``postgres_objects.GeneratedField`` is Django's ``GeneratedField`` plus one promise: when a declared function the
column depends on changes what it computes, the stored values are recalculated (see
:ref:`recalculating-generated-columns`). Django's own ``GeneratedField`` works here too, but its stored values are left
alone on such a change, because bringing them up to date rewrites the whole table and that has to be asked for.

The functions the expression calls are found by walking it. A function that is only called from *inside* another
function's body cannot be seen that way, so name it explicitly:

.. code-block:: python

    name_uppercased = GeneratedField(
        expression=AllUppercase(F('name')),  # whose body calls example_trim
        recalculate_on=(Trim,),  # the declaration class, or its db_name as a string
        output_field=models.TextField(),
        db_persist=True,
    )

Either source will do, and the walk is usually enough on its own: ``recalculate_on`` is for the dependency the
expression cannot show. Built-ins are found by the same walk and resolve to nothing, which is harmless.

The column has to resolve at least one :class:`~postgres_objects.functions.Function` declaration one way or the other,
because one that resolves none can never be recalculated. ``manage.py check`` refuses that, and the way to keep the
field anyway is to say so:

.. code-block:: python

    name_uppercased = GeneratedField(
        expression=Upper(F('name')),  # a built-in, so nothing here is ever recalculated
        recalculate=False,
        output_field=models.TextField(),
        db_persist=True,
    )

``recalculate=False`` switches off the recalculation and every check below with it. It cannot be combined with
``recalculate_on``, since the two say opposite things.

(Alternatively you can use Django's built-in ``GeneratedField`` as well, which has no restrictions on resolving to a
``Function`` declaration.)

============================ ==============================================================================
``postgres_objects.E003``    The field is used, but ``FUNCTIONS_MODULE_PATH`` names no module, so no
                             declaration is managed anywhere in the project.
``postgres_objects.E004``    ``recalculate_on`` names something no declaration is called, such as a typo or a
                             declaration that has since been deleted.
``postgres_objects.E005``    The column is ``VIRTUAL``. See the restriction below: it can never reference a
                             declaration, so it can never be recalculated.
``postgres_objects.E006``    The column resolves no declaration from either source.
============================ ==============================================================================

Two things PostgreSQL requires of a function used this way:

* It must be ``IMMUTABLE``. Nothing here checks that for you; a ``VOLATILE`` function is refused when the column is
  created, not when the declaration is written.
* A generation expression cannot reference a user-defined function at all if the column is ``VIRTUAL`` rather than
  stored. That restriction is PostgreSQL's, and it rules out declared functions for virtual columns entirely, which is
  what ``postgres_objects.E005`` reports.

Once the column exists, it depends on the function, and PostgreSQL will refuse to drop the function while it does. That
is why a changed signature adds the new overload before the model migrations and drops the old function after them: the
migrations in between are where the column moves across.
