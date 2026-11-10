v1.1.0 (2026-11-10)
-------------------
Added:
 * Support for Python 3.12 and 3.13.
 * Support for Python 3.15 (Django 6.1.3 or later).
 * Support for Django 5.2 (5.2.19 or later).
 * Badges to documentation overview.

v1.0.2 (2026-11-09)
-------------------
Added:
 * Coverage badge in the README.

Altered:
 * Fixed the documentation badge in the README rendering as a broken image on GitHub.

v1.0.1 (2026-10-12)
-------------------
Altered:
 * Fixed a minor packaging bug where the sdist would ship coverage data if present.
 * Reorganised documentation into sections for better readability.

v1.0.0 (2026-08-26)
-------------------
Added:
 * Declarative PostgreSQL functions.
 * Declarative PostgreSQL views and materialized views.
 * ``postgres_objects.GeneratedField``, a ``GeneratedField`` whose stored values are recalculated when a declared
   function it depends on changes what it computes.
