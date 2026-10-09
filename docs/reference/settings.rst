========
Settings
========

Every setting lives in the ``POSTGRES_OBJECTS`` dict:

.. code-block:: python

    POSTGRES_OBJECTS = {
        'FUNCTIONS_MODULE_PATH': 'db_functions',
        'VIEWS_MODULE_PATH': 'db_views',
    }

The system check ``postgres_objects.W001`` reports a key the package does not know; see :doc:`/reference/checks`.


FUNCTIONS_MODULE_PATH
---------------------

The module, relative to each app, that function declarations are read from. A dotted path such as ``'db.functions'``
names a module inside a package. When it is not set, no function is managed. See :doc:`/guides/functions`.


VIEWS_MODULE_PATH
-----------------

The module, relative to each app, that view declarations are read from, in the same form as
``FUNCTIONS_MODULE_PATH``. When it is not set, no view is managed. See :doc:`/guides/views`.
