=====
Views
=====

Declare each view in the module named by ``POSTGRES_OBJECTS['VIEWS_MODULE_PATH']``, in the app that owns it:

.. code-block:: python

    # example/db_views.py
    from postgres_objects import View


    class TallCakes(View):
        sql = 'SELECT id, name FROM example_cake WHERE layers > 3'



Attributes
----------

=============== ================================================================================================
``sql``         The ``SELECT`` query the view is defined as. A trailing semicolon is optional. Mutually exclusive with
                ``queryset``.
``queryset``    A method returning the queryset the view selects; see below. Mutually exclusive with ``sql``.
``primary_key`` Names the column the generated model is keyed by, where it cannot be worked out; see below.
``options``     Reloptions, rendered into the ``WITH`` clause, e.g. ``{'security_invoker': 'true'}`` or
                ``{'check_option': 'cascaded'}``. Defaults to none.
``depends_on``  Relations this view reads that compiling cannot discover (e.g. everything a raw-sql body reads). Each
                entry is a ``View`` declaration, a model, or a table name; see below.
=============== ================================================================================================

Three more control naming rather than behaviour, and all three are optional:

=============== ============================================================================================
``name``        Overrides the name, which otherwise comes from the class name, lowercased.
``app_label``   Overrides the owning app, which otherwise comes from the declaring module.
``db_name``     Overrides the identifier the view is created under, otherwise ``'{app_label}_{name}'``.
=============== ============================================================================================


Declaring the body as a queryset
--------------------------------

Instead of raw SQL, a view may declare the queryset it selects:

.. code-block:: python

    from django.db.models import Count
    from postgres_objects import MaterializedView

    from example.models import Cake


    class CakeCounts(MaterializedView):
        unique_index = ('name',)

        def queryset():
            return Cake.objects.values('name').annotate(cakes=Count('id'))


This flavor is made for the common case of masking an existing model: a subset of its columns (e.g.
``.values('id','name')``), a row condition (e.g. ``.filter(archived=False)``), or an aggregate over it. It deliberately
does not support everything raw SQL can say. A combinator (``union()`` and friends), a column Django cannot name, or
anything else out of its reach is refused with an error that says to write the ``sql`` by hand.

For a materialized view, ``unique_index`` and ``indexes`` are checked against the queryset's columns when the definition
is built, so a typo fails at ``makemigrations`` rather than at migrate time.

A queryset also says which tables it reads, which is what the migrations are ordered by; see `What a view reads`_.


Joining models
~~~~~~~~~~~~~~

A view is often the place to flatten a relation, and the queryset traverses it the way any queryset does:

.. code-block:: python

    from django.db.models import F
    from postgres_objects import View

    from bakery.models import Recipe


    class RecipeCredits(View):
        def queryset():
            return Recipe.objects.values('id', baker_name=F('baker__name'), cake_name=F('cake__name'))

Every column has to be named, because the view holds columns and nothing else. Note that ``select_related()`` is not
applicable here, even though you would probably use it if you wrote a queryset for something model-based anywhere else.
Name each column across the join with ``values()`` or ``annotate()``, as above. Be aware that a bare ``values('baker')``
gives the key column ``baker_id``, which may or may not be what you are looking for.

The join tables may belong to another app, as ``example.Cake`` does above.


What is frozen, and when
~~~~~~~~~~~~~~~~~~~~~~~~

The queryset is compiled when ``makemigrations`` runs, and the resulting SQL string is what the migration carries. The
migration never refers back to the queryset or the models it was built from, so it means the same thing regardless of
when and how the declaration changes later. Three consequences:

* Anything evaluated while building the queryset is frozen into the SQL. ``filter(created__gt=datetime.now())`` freezes
  the timestamp of the ``makemigrations`` run; use database-side expressions like ``Now()`` instead.
* An unordered collection in a filter (``__in`` over a ``set``) can compile in a different order each run and will churn
  migrations. Use a sorted sequence.
* A Django upgrade may change the emitted SQL cosmetically. The change detection compares strings, so that shows up as
  one drop-and-recreate migration that changes nothing real. Harmless and hard to avoid.

An aggregate queryset compiles with ``GROUP BY 1``, a position that PostgreSQL accepts and rewrites into the expression
it refers to. This is intentional, please do not fix this in the generated migration file.

The freeze is not only at ``makemigrations`` time. The compiled queryset and the generated model are also cached on
the declaration class for the lifetime of the process, populated at the first touch, which in practice is
``manage.py check`` or startup, since the system checks build every declared view's definition and model. So
``queryset()`` must be deterministic and compile-time-stable: the same SQL every call. A Python-side volatile value
(``timezone.now()``, ``random``, an unordered ``set`` in ``__in``) freezes at the first compile and is silently reused
by every later read in that process; use database-side expressions instead. This caching is by design: a declaration
never changes within a process, and compiling per access would put a full ORM compile on every query path.


Reading the view back
---------------------

A queryset-declared view knows its columns, so it can carry a generated unmanaged model:

.. code-block:: python

    CakeCounts.objects.filter(cakes__gt=3)
    CakeCounts.model  # the model class itself, for serializers and the like

The model is built the first time ``.objects`` or ``.model`` is touched, and kept. If you never use it, there is no
performance hit for this feature, which is why there is no setting controlling this. The unmanaged model lives in a
private app registry, which is what keeps ``makemigrations`` from seeing a table to create and ``migrate`` from handing
it a content type and permissions. This essentially means it is only there for convenient data access, and nothing else
model-like.

The model is keyed by, in order of precedence: the column ``primary_key`` names, a materialized view's single-column
``unique_index``, or an ``id`` column the queryset selects. When none applies, building the model fails with an error
listing the columns (in that case you have to specify the ``primary_key`` yourself manually).

The generated model carries plain data fields only: a foreign key column arrives as the type of the key, not a relation,
and there are no custom managers or methods. Rows pickle by finding their way back to the declaration, so caches and
task queues work. Mistakes will surface through system checks (``postgres_objects.E001``/``E002``).

The manager is an ordinary one, so can be used to build stacked views; see `Views built on views`_.

A raw-``sql`` View has no unmanaged model, since its columns are not knowable from a string.


Hand-written unmanaged models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When you want more than plain columns (e.g. foreign key navigation, custom managers, model methods) write the unmanaged
model yourself. This approach also works for creating an unmanaged model for a raw-``sql`` View:

.. code-block:: python

    class CakeCount(models.Model):
        name = models.CharField(primary_key=True, max_length=128)
        cakes = models.IntegerField()

        class Meta:
            managed = False
            db_table = CakeCounts.resolved_db_name

If you do this for a queryset-based View, the automatically generated model will go unused, which is harmless as long as
you make sure that you consistently use your own in code, and don't mix them.

The standing caveats for any model over a view apply: it is effectively read-only, foreign key fields enforce nothing,
and a view change is a drop-and-recreate, so the model's fields have to move in the same deploy.


A change is a drop and a recreate
---------------------------------

There is no in-place alter for a view. Any change at all drops the old one before the model migrations and creates the
new one after them.

Part of this is a PostgreSQL limitation: ``CREATE OR REPLACE VIEW`` can only append columns to a regular view, and a
materialized view has no replacing form at all. On top of that, a view usually changes because the tables it reads
changed, and a replacement running after the model migrations would be too late: the ``ALTER TABLE`` would already have
failed against the old view still holding on to the column.

Two consequences worth knowing:

* The view does not exist in between the two migrations. They are separate transactions, so there is a real if brief
  gap. If this matters, you will need to manually adjust migrations (either by combining migrations within one app or
  by creating bridge logic with manual ``RunSQL``).
* Grants on the view are lost, since it is a new object. Re-grant in a follow-up operation (out of scope of this
  library).


Views built on views
--------------------

A queryset-declared view carries an unmanaged model, and that model is an ordinary query source. So the easy way to
build a view on top of another one is to select from its manager:

.. code-block:: python

    from postgres_objects import View

    from example.db_views import CakeCounts


    class PopularCakes(View):
        primary_key = 'name'

        def queryset():
            return CakeCounts.objects.filter(cakes__gt=1).values('name', 'cakes')


The view being read has to be queryset-declared for this to work, since a raw-``sql`` view has no model to select from.
If you need to manually write the ``sql`` for a View that depends on another View, name the dependency yourself in
``depends_on`` so the migration autodetector can properly resolve dependencies.


.. _what-a-view-reads:

What a view reads
-----------------

Migration order takes into account inter-view dependencies, including across apps. This means that views are migrated
in order of dependency, not in order of declaration. However, that requires the autodetector to know a view's
dependencies. For queryset-based views this is easy to derive, but raw SQL cannot be asked what it reads without parsing
it, and that is intentionally left out of scope. A raw-``sql`` view thus needs to declare it with ``depends_on``:

.. code-block:: python

    class UppercasedCakes(View):
        sql = 'SELECT id, upper(name) AS name FROM example_cake'
        depends_on = [Cake]


    class StackedCakes(View):
        sql = 'SELECT id FROM example_uppercasedcakes'
        depends_on = [UppercasedCakes]

Each entry is a ``View`` declaration, a model, or a table name as a string. ``depends_on`` is additive, so a queryset
declaration that already automatically derives some dependencies can have more added to by declaring ``depends_on`` as
well.

Two system checks guard this wiring, alongside the ``postgres_objects.E001``/``E002`` checks on queryset declarations:

============================ ==============================================================================
``postgres_objects.E007``    The view declaration depends on something that names no relation: a ``depends_on`` entry
                             that is not a ``View`` declaration, a model or a table name, or one naming an abstract
                             declaration.
``postgres_objects.W001``    The ``POSTGRES_OBJECTS`` setting contains an unknown key. A typo there fails nothing on its
                             own (the key is simply never read) so the module it meant to name would silently not be
                             managed at all. The hint names the closest known key.
============================ ==============================================================================

A raw-``sql`` view without ``depends_on`` has no references at all, and will get ordered in migrations based on
declaration order as a fallback. This may cause migration problems at migrate time (notably *not* noticeable at time of
making the migrations) and if you do see this, you can solve it with ``depends_on``.

The autodetector has one important limitation. Two apps whose views read each other's (one.alpha reading two.beta and
two.charlie reading one.delta) cannot be laid out as one migration per app, and ``makemigrations`` says so rather than
writing files that fail the next time the graph is loaded. The same goes for two views within one app whose
``depends_on`` reference each other. In either case, you need to split the change over two runs.


Materialized views
------------------

A materialized view stores its rows instead of computing them per query:

.. code-block:: python

    from postgres_objects import MaterializedView


    class CakeTotals(MaterializedView):
        sql = 'SELECT name, count(*) AS cakes FROM example_cake GROUP BY name'
        unique_index = ('name',)
        indexes = [('cakes',)]

================= ==============================================================================================
``unique_index``  Columns of a unique index, created with the view. PostgreSQL requires one to refresh concurrently,
                  which is why it is declared separately from the rest.
``indexes``       Further indexes, each a tuple of column names.
``with_data``     ``False`` renders ``WITH NO DATA`` on the ``CREATE``, leaving the view empty for a later refresh to
                  fill. PostgreSQL refuses to read such a view until that refresh has run, and writing that refresh is
                  yours to do. Defaults to ``True``.
================= ==============================================================================================

``CREATE MATERIALIZED VIEW`` copies no indexes of its own, so declaring them here is what keeps them in the migrations
rather than in a hand-written ``RunSQL`` the change detection knows nothing about.


Refreshing
~~~~~~~~~~

Stored rows go stale, and no declaration can say when. Hence refreshing is something you ask for, from wherever you know
the data has moved:

.. code-block:: python

    from example.db_views import CakeTotals

    CakeTotals.refresh()
    CakeTotals.refresh(concurrently=True)

``concurrently=True`` keeps the view readable while it refreshes. PostgreSQL allows that only for a view carrying the
``unique_index`` and populated at least once. The ``unique_index`` half is enforced in Python: the declaration refuses
without one rather than letting the database complain afterwards. The populated-once half deliberately is not: a
declaration cannot know whether the view has been filled, and checking ``with_data`` instead would wrongly refuse every
concurrent refresh after the first fill, since the declaration keeps saying ``with_data=False`` forever. Refresh a
never-filled view concurrently and PostgreSQL itself refuses, with
``ERROR: materialized view "…" has not been populated``. The rule to remember: the first fill of a ``with_data=False``
view must be a plain refresh; concurrent refreshes are for every fill after it.

The same refresh is available as a migration operation, which is where the *first* fill usually belongs for a view
created with ``with_data=False``. The operation takes the complete definition, spelled out in full: a migration never
refers back to the live declaration, so there is no shorthand that fills the fields in for you. Copy the
``MaterializedViewDefinition(...)`` from the ``AddView`` in the generated migration rather than typing it:

.. code-block:: python

    from django.db import migrations

    from postgres_objects.operations import RefreshMaterializedView
    from postgres_objects.views import MaterializedViewDefinition


    class Migration(migrations.Migration):
        operations = [
            RefreshMaterializedView(
                MaterializedViewDefinition(
                    name='caketotals',
                    db_name='example_caketotals',
                    sql='SELECT name, count(*) AS cakes FROM example_cake GROUP BY name',
                    options={},
                    references=('example_cake',),
                    unique_index=('name',),
                    indexes=(),
                    with_data=False,
                ),
            ),
        ]

(``concurrently=True`` is available here too, but never for this first fill, per the rule above: PostgreSQL only
refreshes concurrently a view that has been populated at least once, and this operation is what does the populating.)

Neither refresh is ever written for you. ``makemigrations`` writes the ``AddView`` and stops there: nothing in a
declaration says that a view is waiting to be filled or that its rows have fallen behind, so the autodetector has
nothing to read. Declaring ``with_data=False`` and leaving the operation out of the migration therefore produces a
migration that applies cleanly and a view PostgreSQL will not read, and nothing reports it (the declaration is valid,
``manage.py check`` passes, and the database holds exactly what the migrations asked for). Writing that operation into
the migration is yours to do, as is every refresh after it.

Reversing a refresh does nothing, since there is no earlier set of rows to go back to, and a rollback should not fail
for want of one.


Which connection a refresh runs on
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Similar to operations on models, you can provide ``using`` to specify the exact database connection to use for the
``refresh()`` method:

.. code-block:: python

    CakeTotals.refresh(using='reporting')

When not explicitly provided, ``db_for_refresh()`` decides. If required, you can override this hook on your view:

.. code-block:: python

    class PerOvenTotals(MaterializedView):
        @classmethod
        def db_for_refresh(cls):
            return current_oven()

