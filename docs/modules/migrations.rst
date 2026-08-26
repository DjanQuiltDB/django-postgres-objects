==========
Migrations
==========

What gets generated
-------------------

Function migrations are written *before* that app's model migrations, and removals *after* them. That way, a model
migration adding a generated column can rely on the function its expression calls, and a function is only dropped once
nothing refers to it any more.

Views are the other way around: they are created *after* model migrations, and removed *before* them. This is because a
view reads from tables (i.e. models), and PostgreSQL refuses to alter a column a view depends on.

Within one migration the two kinds nest accordingly: view drops lead the function operations, and view creates follow
the function removals. See :doc:`views`.

Running ``makemigrations`` on an app whose model has a generated column calling the new function ``AllUppercase``
produces two migrations. The first creates the function:

.. code-block:: python

    operations = [
        postgres_objects.operations.AddFunction(
            definition=postgres_objects.functions.FunctionDefinition(
                name='alluppercase',
                db_name='example_alluppercase',
                arguments='input TEXT',
                returns='TEXT',
                ...
            ),
        ),
    ]

The second creates the model and depends on the first. Note that the migration carries a ``FunctionDefinition`` with the
values spelled out, it does not import ``example.db_functions``. Editing the declaration later cannot rewrite what this
migration means. (This is similar to how model state is carried inside a model migration by Django itself.)

Where a model in one app uses a function declared in another, every app's first model migration is made to depend on
every app's object migration, and every removal migration waits for every app's last regular migration. Removals cannot
know which app held the last reference, so they wait for all of them.

Views are wired more precisely than that, because a view explicitly says what it reads, and this gives the autodetector
more useful information. Each ``ViewDefinition`` carries the tables its ``SELECT`` names, compiled from the queryset or
given as ``depends_on``, and each becomes a dependency on the migration that puts that relation in place: another app's
view migration of the same run, the migration that created the view in an earlier run, or the latest migration of an app
whose models did not change at all. Drops are wired the same way in reverse (the app owning what is read waits for the
app dropping the reader).

The blanket rules above still stand alongside these. They cover functions and generated columns, which have no reference
set of their own, and a reference this package cannot resolve (e.g. a table an extension owns, or one created by hand)
is ignored for dependency resolution purposes.

One operation is **never** generated at all: a refresh of a materialized view. Since the declaration is only providing
information about structure, not about content, there is nothing for the autodetector to act on. A view declared with
``with_data=False`` gets its ``AddView`` and nothing more. If you want to refresh from the migration you can manually
add a ``RefreshMaterializedView`` operation; alternatively, call ``refresh()`` on the view from the application. See
:doc:`views` for more information.


How a change is classified
--------------------------

Not every edit can be applied the same way, because PostgreSQL will not always let the old and new function coexist.

If the body changed, with the signature and return type untouched: ``CREATE OR REPLACE FUNCTION`` handles this in
place, so an ``AlterFunction`` runs before the model migrations. It carries the previous definition too, which is what
makes it reversible. A stored generated column computed by the function keeps its old values across this change;
:ref:`recalculating-generated-columns` below is what brings them up to date.

If the return type changed, with the signature unchanged: ``CREATE OR REPLACE`` refuses this, and an identical
signature means there is no overload to hide behind, so a ``RemoveFunction`` and an ``AddFunction`` both run before the
model migrations. Anything still depending on the old function will block the drop on purpose; migrate the dependent off
it first.

If the signature changed: the new function is a different overload, so the two can coexist. The ``AddFunction`` runs
before the model migrations and the ``RemoveFunction`` after them, leaving the model migrations in between as the place
to move a dependent generated column across.

If a view changed (regardless of how): views have no in-place case, so the old one is dropped before the model
migrations and the new one created after them. :doc:`views` explains why ``CREATE OR REPLACE VIEW`` is not enough.


Operations
----------

``AddFunction(definition, hints=None)``, ``AlterFunction(definition, previous, hints=None)`` and
``RemoveFunction(definition, hints=None)``. All three are reversible; ``AlterFunction`` reverses to ``previous``.

``AddView(definition, hints=None)`` and ``RemoveView(definition, hints=None)``, reversible the same way, and
``RefreshMaterializedView(definition, concurrently=False, hints=None)``, whose reverse deliberately does nothing (see
:doc:`views`).

``RecalculateGeneratedField(model_name, name, hints=None)`` belongs to a model rather than to a declared object; see
:ref:`recalculating-generated-columns` below.

``hints`` is what the operation hands to the database routers' ``allow_migrate``, recorded from the declaration's
``router_hints`` when the migration was written. An operation whose recorded hints are empty defers to
``DeclarativeObject.router_hints`` at apply time.

.. _recalculating-generated-columns:

Recalculating stored generated columns
--------------------------------------

PostgreSQL computes a stored generated column when a row is written, not when a function behind it changes. On a
function body change, every existing row keeps whatever the old body computed; only rows written afterwards use the new
one. Nothing about the column's definition says which rows are which.

Declaring the column with ``postgres_objects.GeneratedField`` (see :doc:`queries`) solves this in the autodetector.
Whenever a declared function the column depends on changes what it computes, the trailing object migration gets a
recalculation:

.. code-block:: python

    postgres_objects.operations.RecalculateGeneratedField(
        model_name='Cake',
        name='name_uppercased',
    ),

The operation runs ``ALTER TABLE ... ALTER COLUMN ... SET EXPRESSION AS (...)`` with the expression the column already
has, which is PostgreSQL's way of forcing the recomputation. Note that:

* It rewrites the whole table, under an ``ACCESS EXCLUSIVE`` lock, like any table rewrite. On a large table, plan for it
  the way you would plan any rewriting ``ALTER TABLE``.
* It lands *after* the function change. The recalculation goes in the trailing object migration of the *model's* app,
  which waits for every app's object migrations, so the rewrite always sees the new body, even when the function is
  declared in a different app, and also when nothing else changed at all.

A change to only ``volatility`` or ``parallel`` recalculates nothing: those are promises to the planner and cannot
change a stored value, so they are not worth a table rewrite. A changed return type or signature does not produce a
recalculation either, since the column cannot survive such a change unaltered, and will always come with a
drop-and-recreate that the dependent column blocks outright (and a signature change means the column's own expression
has to change, since Django refuses to alter a ``GeneratedField`` in place, so the field is removed and re-added, and
the ``ADD COLUMN`` computes every row with the new function as it goes).

Unapplying is asymmetric, on purpose. Reversing the plan restores the old function body *after* the recalculation
migration has been unapplied, so the operation's reverse cannot usefully recompute anything and does nothing instead.
Rolled back, the table keeps values the newer body computed. If this matters and you wish to adjust this anyway, write a
``RecalculateGeneratedField`` by hand in a follow-up migration or ``refresh()`` the view from application code or the
shell.

Of course virtual generated columns do not have this problem and will always provide values calculated with the current
function value.


Inner workings details
----------------------

Changes are detected by the autodetector by folding the object operations over the migration graph provided by Django's
own autodetector. Every ``AddFunction`` and ``AlterFunction`` records a definition and every ``RemoveFunction`` removes
one, in migration order; the result is what the migrations have created so far, and that is what the declarations are
compared against.

A function or view created by a hand-written ``RunSQL`` is invisible to the comparison, and so is one changed by hand in
a database. Only what these operations put there is tracked. If you have pre-existing objects that you want to adopt
in declarative classes with this library, write the exactly matching definitions once and either ``migrate --fake`` the
resulting initial migration for them, or use ``SeparateDatabaseAndState`` to prevent the operations from touching the
existing objects.

Squashing is not specialised: the operations implement no ``reduce()``, so a squashed migration keeps each of them
rather than collapsing a create-then-alter into a single create. That includes ``RecalculateGeneratedField``, which is
harmless to replay: on a database built from scratch, the table is empty when it runs, so the rewrite costs nothing.
However, if you accumulate a lot of these operations over time, we do recommend you manually scrub obsolete operations
from squashed migrations for better maintainability.
