============
Installation
============

.. code-block:: shell

    pip install django-postgres-objects

Add it to ``INSTALLED_APPS``:

.. code-block:: python

    INSTALLED_APPS = (
        ...
        'postgres_objects',
        ...
    )

Then name the module each kind of declaration lives in, relative to each app:

.. code-block:: python

    POSTGRES_OBJECTS = {
        'FUNCTIONS_MODULE_PATH': 'db_functions',
        'VIEWS_MODULE_PATH': 'db_views',
    }

Every app may now have a ``db_functions.py`` and a ``db_views.py``, and ``makemigrations`` will manage what it finds
there. A path may be dotted, so ``'db.functions'`` works too if you would rather declarations lived in a package.

If you want to use only one feature, you can leave the path for the other undefined. For example, if you prefer to use
another package to handle views, and only want to use this package for functions, configure it as such:

.. code-block:: python

    POSTGRES_OBJECTS = {
        'FUNCTIONS_MODULE_PATH': 'db_functions',
    }

See :doc:`/reference/settings` for both keys. A project that also uses another library extending ``makemigrations`` or
``migrate`` may need one more step; see :doc:`/integrations/migration_libraries`.


Requirements
------------

* Python 3.14
* Django 6.0 or 6.1
* PostgreSQL 17 or 18
