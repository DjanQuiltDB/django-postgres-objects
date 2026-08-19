"""
Declarative PostgreSQL views and materialized views.
"""

from types import FunctionType

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections, router
from django.db.backends.utils import truncate_name
from django.db.models import Model

from postgres_objects.base import (
    MAX_IDENTIFIER_LENGTH,
    Change,
    DeclarativeObject,
    DeclarativeObjectMeta,
    ObjectDefinition,
)
from postgres_objects.operations import AddView, RemoveView
from postgres_objects.querysets import build_model, compile_queryset

VIEW_CREATE_SQL = 'CREATE OR REPLACE VIEW {db_name}{options} AS {sql};'
MATERIALIZED_VIEW_CREATE_SQL = 'CREATE MATERIALIZED VIEW {db_name}{options} AS {sql}{with_data};'
REFRESH_SQL = 'REFRESH MATERIALIZED VIEW{concurrently} {db_name};'


def check_refresh(db_name, unique_index, concurrently):
    """
    Refuse a refresh PostgreSQL would refuse, in the one place the migration operation and the runtime call meet.

    Only the half a declaration can know is checked. PostgreSQL also refuses a concurrent refresh of a view never
    populated, but that is runtime state: reading ``with_data`` for it would wrongly refuse every concurrent refresh
    after the first fill. That first fill of a ``with_data=False`` view has to be a plain refresh.
    """
    if concurrently and not unique_index:
        raise ValueError(
            'Refreshing {} concurrently needs a unique_index on the declaration, which is what PostgreSQL requires '
            'to refresh without locking readers out.'.format(db_name)
        )


def build_refresh_sql(db_name, unique_index, concurrently=False):
    """
    The refresh statement, from the two things it actually needs: the identifier and the unique index the check wants
    to see. Working from so little is what lets refresh() run without compiling the declaration's body.
    """
    check_refresh(db_name, unique_index, concurrently)

    return REFRESH_SQL.format(concurrently=' CONCURRENTLY' if concurrently else '', db_name=db_name)


def resolve_reference(reference):
    """
    The table name a ``depends_on`` entry refers to.

    A View declaration or a model class is accepted for readability and normalized to the identifier it is created
    under; a plain string is taken as that identifier itself. Anything else is refused outright rather than left to fail
    as an AttributeError further along, the same way recalculate_on entries are handled in fields.py.
    """
    if isinstance(reference, str):
        return reference

    if isinstance(reference, type):
        if issubclass(reference, Model):
            return reference._meta.db_table

        if isinstance(reference, DeclarativeObjectMeta) and issubclass(reference, View):
            if reference.abstract:
                raise ImproperlyConfigured('{} is abstract, so it names no relation.'.format(reference.__name__))

            return reference.resolved_db_name

    raise ImproperlyConfigured(
        'depends_on takes View declarations, models or table names as strings, not {!r}.'.format(reference)
    )


def format_options(options):
    """
    Render reloptions as the WITH clause of a CREATE, or nothing at all when there are none.
    """
    if not options:
        return ''

    return ' WITH ({})'.format(', '.join('{}={}'.format(key, options[key]) for key in sorted(options)))


class ViewDefinition(ObjectDefinition):
    """
    Everything about a view that ends up in SQL. This is what migrations serialize.
    """

    fields = ('name', 'db_name', 'sql', 'options', 'references')

    # References describe the SQL rather than adding to it, and a definition written by hand should not have to spell
    # out something it can say with the sql alone.
    defaults = {'references': ()}

    # A view reads from tables, so unlike a function it is created after the model migrations and dropped before them.
    precedes_models = False

    kind = 'view'
    object_noun = 'view'
    add_operation_class = AddView
    # No alter operation: see plan_change_from.
    remove_operation_class = RemoveView

    materialized = False

    def create_sql(self):
        return VIEW_CREATE_SQL.format(
            db_name=self.db_name,
            options=format_options(self.options),
            sql=self.sql.strip().rstrip(';'),
        )

    def drop_sql(self, schema_name):
        # No CASCADE: dropping whatever was built on top of this view would take with it an object that nothing here
        # would ever recreate. An ordering mistake should raise instead.
        return 'DROP VIEW IF EXISTS "{schema}".{db_name};'.format(schema=schema_name, db_name=self.db_name)

    def plan_change_from(self, previous):
        """
        Any change at all is a SUPERSEDE, which drops the old view before the model migrations and creates the new one
        after them.

        There is deliberately no in-place ALTER. ``CREATE OR REPLACE VIEW`` can only append columns, since it refuses to
        rename or retype an existing one, and a materialized view has no replacing form at all. More importantly, a
        change to a view usually accompanies a change to the tables it reads, and a replacement running after the model
        migrations would be too late: the ALTER TABLE would already have failed against the old view still depending on
        the column. Splitting the change around the model migrations is what makes that case work.
        """
        if previous == self:
            return Change.UNCHANGED

        return Change.SUPERSEDE


class MaterializedViewDefinition(ViewDefinition):
    """
    A view whose rows are stored rather than computed per query.
    """

    fields = ('name', 'db_name', 'sql', 'options', 'references', 'unique_index', 'indexes', 'with_data')

    object_noun = 'materialized view'
    materialized = True

    def index_name(self, columns, unique):
        return truncate_name(
            '{}_{}_{}'.format(self.db_name, '_'.join(columns), 'key' if unique else 'idx'), MAX_IDENTIFIER_LENGTH
        )

    def index_sql(self, columns, unique):
        # The columns are quoted because a compiled queryset quotes its aliases: a mixed-case alias is created exactly
        # as written, and bare in the CREATE INDEX it would fold to lowercase and miss. The index name is quoted for
        # the same reason, since the column names are part of it. The view's own db_name stays bare, matching how the
        # CREATE and DROP of the view itself name it.
        return 'CREATE{unique} INDEX IF NOT EXISTS "{name}" ON {db_name} ({columns});'.format(
            unique=' UNIQUE' if unique else '',
            name=self.index_name(columns, unique),
            db_name=self.db_name,
            columns=', '.join('"{}"'.format(column) for column in columns),
        )

    def create_sql(self):
        return MATERIALIZED_VIEW_CREATE_SQL.format(
            db_name=self.db_name,
            options=format_options(self.options),
            sql=self.sql.strip().rstrip(';'),
            with_data='' if self.with_data else ' WITH NO DATA',
        )

    def create_statements(self):
        """
        The CREATE, then an index per declared index.

        CREATE MATERIALIZED VIEW copies no indexes of its own, and without a unique one the view cannot be refreshed
        concurrently, so the indexes have to be declared alongside the view rather than left to a hand-written RunSQL.
        """
        statements = [self.create_sql()]

        if self.unique_index:
            statements.append(self.index_sql(self.unique_index, unique=True))

        for columns in self.indexes:
            statements.append(self.index_sql(columns, unique=False))

        return tuple(statements)

    def drop_sql(self, schema_name):
        return 'DROP MATERIALIZED VIEW IF EXISTS "{schema}".{db_name};'.format(schema=schema_name, db_name=self.db_name)

    def check_refresh(self, concurrently):
        """
        The module-level check over this definition's fields. Kept as a method because the refresh operation calls it
        when the migration is written.
        """
        check_refresh(self.db_name, self.unique_index, concurrently)

    def refresh_sql(self, concurrently=False):
        return build_refresh_sql(self.db_name, self.unique_index, concurrently)


class ViewMeta(DeclarativeObjectMeta):
    def __new__(mcs, class_name, bases, namespace, **kwargs):
        cls = super().__new__(mcs, class_name, bases, namespace, **kwargs)

        # queryset() is written without self, because a declaration is never instantiated. Reached through the class a
        # plain function is already called that way, so wrapping it changes nothing about how it runs; the wrapper only
        # states it.
        queryset = namespace.get('queryset')
        if isinstance(queryset, FunctionType):
            cls.queryset = staticmethod(queryset)

        return cls

    def check_body(cls):
        """
        Refuse a declaration whose body is missing or said twice, in the one place both flavors meet.
        """
        if cls.abstract:
            raise TypeError('{} is abstract, so it has no definition.'.format(cls.__name__))

        if cls.sql and cls.queryset:
            raise TypeError(
                '{} declares both sql and queryset, so what the view selects is said twice. Keep one of them.'.format(
                    cls.__name__
                )
            )

        if not cls.sql and not cls.queryset:
            raise TypeError(
                '{} declares neither sql nor queryset, so there is nothing to select from.'.format(cls.__name__)
            )

    @property
    def compiled(cls):
        """
        The declared queryset, compiled once and kept for the lifetime of the process, by design, so queryset() must
        return the same SQL every call (see "What is frozen, and when" in the views docs).

        Cached on this very class rather than looked up through inheritance, because a subclass is a concrete view of
        its own whose columns must be its own.
        """
        if 'compiled_queryset' not in cls.__dict__:
            cls.compiled_queryset = compile_queryset(cls.queryset())

        return cls.compiled_queryset

    @property
    def resolved_sql(cls):
        """
        The SELECT this view is defined as, compiled from the declared queryset when there is one.

        Compiling is deferred to here, so importing a declarations module costs nothing and the module can import its
        app's models at the top: nothing is asked of the ORM until a definition or a model is first needed.
        """
        cls.check_body()

        if cls.sql:
            return cls.sql

        return cls.compiled.sql

    @property
    def references(cls):
        """
        The tables this view reads, as the identifiers its SQL names them by.
        """
        cls.check_body()

        derived = cls.compiled.tables if cls.queryset else ()
        declared = (resolve_reference(reference) for reference in cls.depends_on)

        return tuple(sorted((set(derived) | set(declared)) - {cls.resolved_db_name}))

    @property
    def model(cls):
        """
        A model over this view's columns, for reading it back through the ORM.

        Built when first asked for and kept for the lifetime of the process, so a declaration whose model is never
        touched costs nothing and every read shares one class.
        """
        cls.check_body()

        if not cls.queryset:
            raise TypeError(
                '{} declares raw sql, whose columns are not knowable from here, so it has no model. Declare a '
                'queryset() instead, or write an unmanaged model against {} by hand.'.format(
                    cls.__name__, cls.resolved_db_name
                )
            )

        if 'generated_model' not in cls.__dict__:
            cls.generated_model = build_model(cls, cls.compiled)

        return cls.generated_model

    @property
    def objects(cls):
        return cls.model._default_manager

    @property
    def definition(cls):
        return ViewDefinition(
            name=cls.name,
            db_name=cls.resolved_db_name,
            sql=cls.resolved_sql,
            options=dict(cls.options or {}),
            references=cls.references,
        )


class MaterializedViewMeta(ViewMeta):
    def check_indexes(cls):
        """
        Refuse an index over a column the queryset does not select.

        Only possible for the queryset flavor, whose columns are known here; with raw sql the first word comes from
        PostgreSQL at migrate time.
        """
        if not cls.queryset:
            return

        names = cls.compiled.column_names
        for columns in filter(None, (cls.unique_index, *cls.indexes)):
            for column in columns:
                if column not in names:
                    raise TypeError(
                        "{} indexes '{}', but its queryset selects {}.".format(cls.__name__, column, ', '.join(names))
                    )

    @property
    def definition(cls):
        cls.check_body()
        cls.check_indexes()

        return MaterializedViewDefinition(
            name=cls.name,
            db_name=cls.resolved_db_name,
            sql=cls.resolved_sql,
            options=dict(cls.options or {}),
            references=cls.references,
            unique_index=tuple(cls.unique_index or ()),
            indexes=tuple(tuple(columns) for columns in cls.indexes),
            with_data=cls.with_data,
        )


class View(DeclarativeObject, metaclass=ViewMeta):
    """
    A declared PostgreSQL view, managed as migration operations instead of raw SQL.

    :Example:
        .. code-block:: python

            from postgres_objects import View


            class TallCakes(View):
                sql = 'SELECT id, name FROM example_cake WHERE layers > 3'

    View migrations are written *after* that app's model migrations and dropped *before* them because a view reads from
    tables rather than being read by them.
    """

    abstract = True

    #: The SELECT the view is defined as. A trailing semicolon is optional. Exactly one of this and `queryset`.
    sql = None

    #: A method returning the queryset the view selects. Written without self, since a declaration is never
    #: instantiated, and called only when the definition or the model is first needed, which is what lets the declaring
    #: module import its app's models at the top. Exactly one of this and `sql`.
    queryset = None

    #: Names the column the generated model is keyed by, where it cannot be worked out: it falls back to a
    #: materialized view's single-column unique_index, and then to an id column.
    primary_key = None

    #: Reloptions, rendered into the CREATE's WITH clause, e.g. {'security_invoker': 'true'} or
    #: {'check_option': 'cascaded'}.
    options = None

    #: Relations this view reads that compiling cannot discover: everything a raw-sql body reads, above all. Each
    #: entry is a View declaration, a model, or a table name as a string. Additive to what a queryset says itself.
    depends_on = ()


class MaterializedView(View, metaclass=MaterializedViewMeta):
    """
    A declared PostgreSQL materialized view, whose rows are stored and refreshed rather than computed per query.

    :Example:
        .. code-block:: python

            from postgres_objects import MaterializedView


            class CakeTotals(MaterializedView):
                sql = 'SELECT baker_id, count(*) AS cakes FROM example_cake GROUP BY baker_id'
                unique_index = ('baker_id',)

    Populate it again with :meth:`refresh`, or with the ``RefreshMaterializedView`` operation where the fill belongs in
    a migration. Neither is ever autodetected: whether the stored rows are stale is not something a declaration can
    know.
    """

    abstract = True

    #: Columns of the unique index every concurrent refresh needs. Declared separately from `indexes` because that is
    #: the whole reason it matters.
    unique_index = None

    #: Further indexes, each a tuple of column names.
    indexes = ()

    #: Whether to populate the view as it is created, rendered as WITH NO DATA when it is False. A large view is often
    #: cheaper to create empty and refresh later, at the price of being unreadable until that refresh runs.
    with_data = True

    @classmethod
    def db_for_refresh(cls):
        """
        Which connection :meth:`refresh` runs on when it is not told one.

        A declared queryset reads a model the project's routers already know, so that model's ``db_for_write`` answer is
        the placement to follow. This is the same reasoning by which compiling routes through the queryset's own
        connection. A raw-sql body names tables and nothing else, so there is nothing to ask a router about and the
        default connection stands.

        The view's *own* generated model is deliberately not what is asked about: it lives in a private app registry, so
        a router resolving placement by looking a model up would not find it.
        """
        if not cls.queryset:
            return DEFAULT_DB_ALIAS

        return router.db_for_write(cls.queryset().model, **cls.router_hints)

    @classmethod
    def refresh(cls, concurrently=False, using=None):
        """
        Repopulate the view now, from ordinary code rather than from a migration.

        Stored rows go stale as the tables underneath them change, and when that happens is not something a migration
        can be written for: after an import, on a schedule, at the end of a task.

        ``concurrently`` keeps the view readable while it refreshes, which PostgreSQL allows only for a view carrying a
        unique index and populated at least once, so the first fill of a ``with_data=False`` view must be a plain
        refresh, and only the fills after it may be concurrent.

        ``using`` names the connection outright, and is handed to ``connections`` untouched. Anything that indexes it
        works, not only a string alias. Left out, :meth:`db_for_refresh` decides.

        The statement is built from the identifier and the unique index alone. The body is never compiled here, so a
        refresh stays cheap on a hot path and works whether or not the declaration still compiles.
        """
        if cls.abstract:
            raise TypeError('{} is abstract, so there is no view to refresh.'.format(cls.__name__))

        sql = build_refresh_sql(cls.resolved_db_name, tuple(cls.unique_index or ()), concurrently)

        with connections[using if using is not None else cls.db_for_refresh()].cursor() as cursor:
            # params=None so a literal % in the identifier is not read as a placeholder.
            cursor.execute(sql, None)
