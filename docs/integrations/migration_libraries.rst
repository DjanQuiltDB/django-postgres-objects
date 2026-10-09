=========================
Other migration libraries
=========================

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
