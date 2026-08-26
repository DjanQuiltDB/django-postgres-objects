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




Other libraries that extend the autodetector
--------------------------------------------

A library that layers itself onto the ``migrate``/``makemigrations`` management commands the same way this library does,
through extending the autodetector (such as `django-pgtrigger <https://django-pgtrigger.readthedocs.io/>`_), will
coexist with this library without manual setup required. Both contributions end up on one autodetector, whichever order
the two apps are listed in.

However, a library that instead ships its own ``makemigrations`` or ``migrate`` commands will need manual work to exist
alongside this library, because only one app can provide a command: the one listed first in ``INSTALLED_APPS`` wins and
every other version is dropped. In order to use both the other library's management command and this library's
autodetector extensions, subclass that library's command in an app of your own, listed before both, and have it name the
composed autodetector:

.. code-block:: python

    # myproject/db/management/commands/makemigrations.py
    from other_library.management.commands.makemigrations import Command as TheirMakeMigrations

    from postgres_objects.autodetector import get_autodetector


    class Command(TheirMakeMigrations):
        autodetector = get_autodetector(TheirMakeMigrations.autodetector)


Write the same two lines for ``migrate``, against that library's ``migrate``. Both commands have to name the *same*
object, because Django's ``commands.E001`` check refuses to start when ``makemigrations`` and ``migrate`` disagree and
it compares them by identity.


Requirements
------------

* Python 3.14
* Django 6.0 or 6.1
* PostgreSQL 17 or 18
