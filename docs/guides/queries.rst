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

The same expression can be a generated column's expression; see :doc:`/guides/generated_columns`.


Querying declared views
-----------------------

A view declared as a queryset serves its reads the same way: the declaration exposes ``.objects``, the manager of a
model generated from the queryset's columns:

.. code-block:: python

    from example.db_views import CakeCounts

    CakeCounts.objects.filter(cakes__gt=3).order_by('-cakes')

That model is deliberately not part of any installed app, so ``makemigrations`` never mistakes the view for a table to
create. See :doc:`/guides/views` for how the model is built, how its key is chosen, and when to write an unmanaged
model by hand instead.

The manager is an ordinary one, so a queryset over it is also a body another view can be declared as, which is how a
view is built on top of a view, and how the dependency between the two comes to be known. See :doc:`/guides/views`
again.

A materialized view is read the same way, but what it holds is only as fresh as its last refresh, and asking for one is
a runtime call like any other:

.. code-block:: python

    CakeCounts.refresh(concurrently=True)

Both body flavors have it, raw ``sql`` included, since repopulating a view needs nothing but its name. See
:doc:`/guides/views` for what ``concurrently`` requires and which connection the refresh runs on.
