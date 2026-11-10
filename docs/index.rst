django-postgres-objects
=======================
Manage PostgreSQL objects (functions and views) similar to Django models by declaring them class-based
and letting the Django migration framework handle changes.

Release |version|.

.. container:: badges

    .. image:: https://github.com/DjanQuiltDB/django-postgres-objects/actions/workflows/ci.yml/badge.svg
        :target: https://github.com/DjanQuiltDB/django-postgres-objects/actions/workflows/ci.yml
        :alt: CI

    .. image:: https://img.shields.io/codecov/c/github/DjanQuiltDB/django-postgres-objects/master
        :target: https://app.codecov.io/gh/DjanQuiltDB/django-postgres-objects
        :alt: Coverage

    .. image:: https://img.shields.io/pypi/v/django-postgres-objects.svg
        :target: https://pypi.org/project/django-postgres-objects/
        :alt: django-postgres-objects on PyPI

    .. image:: https://img.shields.io/pypi/pyversions/django-postgres-objects.svg
        :target: https://pypi.org/project/django-postgres-objects/
        :alt: Supported Python versions

    .. image:: https://img.shields.io/pypi/frameworkversions/django/django-postgres-objects.svg
        :target: https://pypi.org/project/django-postgres-objects/
        :alt: Supported Django versions

    .. image:: https://img.shields.io/badge/postgres-17%20%7C%2018-4169e1?logo=postgresql&logoColor=white
        :target: https://www.postgresql.org/
        :alt: Supported PostgreSQL versions

    .. image:: https://img.shields.io/pypi/l/django-postgres-objects.svg
        :target: https://github.com/DjanQuiltDB/django-postgres-objects/blob/master/LICENSE
        :alt: BSD-3-Clause licence


.. toctree::
    :maxdepth: 2
    :caption: Getting started

    getting_started/overview
    getting_started/installation

.. toctree::
    :maxdepth: 2
    :caption: Guides

    guides/functions
    guides/views
    guides/generated_columns
    guides/queries
    guides/migrations

.. toctree::
    :maxdepth: 2
    :caption: Integrations

    integrations/migration_libraries
    integrations/djanquiltdb

.. toctree::
    :maxdepth: 2
    :caption: Reference

    reference/settings
    reference/checks
    reference/api
    reference/changelog


Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
