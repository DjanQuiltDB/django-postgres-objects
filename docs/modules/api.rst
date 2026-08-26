=============
API reference
=============

.. currentmodule:: postgres_objects


Declarations
============

The classes you subclass in an app's declaration modules. A declaration is never instantiated: the metaclass reads the
class body and derives the object's name, its app and its database name from it.

.. autoclass:: Function
    :members:

.. autoclass:: View
    :members:

.. autoclass:: MaterializedView
    :members:

.. autoclass:: DeclarativeObject
    :members:


Fields
======

.. autoclass:: GeneratedField
    :members:


Definitions
===========

The frozen, deconstructable value objects that migrations serialize. Written into migration files, so they are stable
history rather than a view onto the current declarations.

.. autoclass:: ObjectDefinition
    :members:

.. autoclass:: FunctionDefinition
    :members:

.. autoclass:: ViewDefinition
    :members:

.. autoclass:: MaterializedViewDefinition
    :members:

.. autoclass:: Change
    :members:


Autodetection
=============

Only needed when another library ships its own ``makemigrations`` or ``migrate`` command and you have to compose the two
autodetectors by hand. See :doc:`installation` for when that applies.

.. currentmodule:: postgres_objects.autodetector

.. autofunction:: get_autodetector

.. autofunction:: compose
