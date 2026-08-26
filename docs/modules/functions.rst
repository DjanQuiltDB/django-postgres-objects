=========
Functions
=========

Declare each function in the module named by ``POSTGRES_OBJECTS['FUNCTIONS_MODULE_PATH']``, in the app that owns it:

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



Attributes
----------

=============== ============================================================================================
``arguments``   The argument list, verbatim, as PostgreSQL spells it. Defaults to no arguments.
``returns``     The return type.
``body``        The function body. Emitted verbatim, so a literal ``%`` needs no escaping.
``language``    Defaults to ``'plpgsql'``.
``volatility``  ``'VOLATILE'`` (the default), ``'STABLE'`` or ``'IMMUTABLE'``.
``strict``      ``True`` emits ``STRICT``. Defaults to ``False``.
``parallel``    ``'UNSAFE'`` (the default), ``'RESTRICTED'`` or ``'SAFE'``.
=============== ============================================================================================

Three more control naming rather than behaviour, and all three are optional:

=============== ============================================================================================
``name``        Overrides the name, which otherwise comes from the class name, lowercased.
``app_label``   Overrides the owning app, which otherwise comes from the declaring module.
``db_name``     Overrides the identifier the function is created under, otherwise ``'{app_label}_{name}'``.
=============== ============================================================================================

``returns`` and ``body`` are required on a concrete declaration: one missing either refuses to build its definition
with a ``TypeError`` naming the class, which is what ``makemigrations`` runs into. A system check surfaces the same
mistake earlier still, at ``manage.py check``:

============================ ==============================================================================
``postgres_objects.E008``    The function declaration could not be built. Its ``returns`` or ``body`` is missing, or
                             building its definition raised.
============================ ==============================================================================


Naming
------

``AllUppercase`` in the ``example`` app is created as ``example_alluppercase``. Namespacing by app is what keeps two
apps from colliding in a schema that has no other namespace to offer, and it follows Django's own convention for model
table names.

PostgreSQL caps identifiers at 63 bytes, and a generated name that would exceed it is truncated. If you need a specific
identifier (e.g. because something outside your migrations refers to it by name) pin it with ``db_name``.

``db_name`` follows the quoting rules of a model's ``Meta.db_table``: the stored name stays as written, and the CREATE
and DROP statements quote it, so an uppercase letter makes the identifier case-sensitive and an already-quoted name
passes through as is. Keep function ``db_name``\ s lowercase, though: querysets and generated-column expressions call
the function through Django's ``Func``, which renders the name unquoted, and Postgres folds that call to lowercase.


Overloads
---------

Two declarations may not share a ``db_name``, so a set of steady-state overloads cannot be declared. One declaration
describes one function. (Overloads do occur *transiently*: a signature change creates the new overload before the model
migrations and drops the old one after them, but that pair never outlives the run that wrote it.)

If you need permanent extra overloads next to a declared function, manage them with plain ``RunSQL`` migrations. This
library ignores what ``RunSQL`` creates, so the two coexist without the change detection reading one as the other.


Reusing a body
--------------

A declaration can be subclassed. Mark the shared part ``abstract`` so it is never created as an object of its own:

.. code-block:: python

    class TextTransform(Function):
        abstract = True

        arguments = 'input TEXT'
        returns = 'TEXT'
        volatility = 'IMMUTABLE'
        strict = True


    class AllUppercase(TextTransform):
        body = """
            BEGIN
                RETURN UPPER(input);
            END;
        """


The abstract base may live anywhere, including outside every installed app. The name and the owning app are taken from
the concrete subclass's own class and module, so a shared base in a utility package attributes nothing to itself.


Renaming
--------

Renaming the class renames the function, because the identifier is derived from the class name. That is usually what you
want, and it produces a drop and a create.

To rename the class *without* touching the database, pin the old identifier:

.. code-block:: python

    class Uppercase(Function):
        db_name = 'example_alluppercase'
        ...

The change detection recognises that the identifier already exists and treats it as the same object, so the live
function is altered rather than dropped and recreated.
