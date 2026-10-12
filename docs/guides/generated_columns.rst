=================
Generated columns
=================

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

``recalculate=False`` switches off the recalculation and every check on the field with it. It cannot be combined with
``recalculate_on``, since the two say opposite things.

(Alternatively you can use Django's built-in ``GeneratedField`` as well, which has no restrictions on resolving to a
``Function`` declaration.)

The checks on the field are ``postgres_objects.E003`` to ``postgres_objects.E006``; see :doc:`/reference/checks`.

Two things PostgreSQL requires of a function used this way:

* It must be ``IMMUTABLE``. Nothing here checks that for you; a ``VOLATILE`` function is refused when the column is
  created, not when the declaration is written.
* A generation expression cannot reference a user-defined function at all if the column is ``VIRTUAL`` rather than
  stored. That restriction is PostgreSQL's, and it rules out declared functions for virtual columns entirely, which is
  what ``postgres_objects.E005`` reports.

Once the column exists, it depends on the function, and PostgreSQL will refuse to drop the function while it does. That
is why a changed signature adds the new overload before the model migrations and drops the old function after them: the
migrations in between are where the column moves across.
