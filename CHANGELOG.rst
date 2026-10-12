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
