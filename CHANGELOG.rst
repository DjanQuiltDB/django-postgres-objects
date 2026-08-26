v1.0.0 (2026-08-26)
-------------------
Added:
 * Declarative PostgreSQL functions.
 * Declarative PostgreSQL views and materialized views.
 * ``postgres_objects.GeneratedField``, a ``GeneratedField`` whose stored values are recalculated when a declared
   function it depends on changes what it computes.
