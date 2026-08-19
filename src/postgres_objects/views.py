"""
Declarative PostgreSQL views and materialized views.
"""

from django.db.backends.utils import truncate_name

from postgres_objects.base import (
    MAX_IDENTIFIER_LENGTH,
    Change,
    DeclarativeObject,
    DeclarativeObjectMeta,
    ObjectDefinition,
)
from postgres_objects.operations import AddView, RemoveView

VIEW_CREATE_SQL = 'CREATE OR REPLACE VIEW {db_name}{options} AS {sql};'
MATERIALIZED_VIEW_CREATE_SQL = 'CREATE MATERIALIZED VIEW {db_name}{options} AS {sql}{with_data};'


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

    fields = ('name', 'db_name', 'sql', 'options')

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

    fields = ('name', 'db_name', 'sql', 'options', 'unique_index', 'indexes', 'with_data')

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

    def refresh_sql(self, concurrently=False):
        return 'REFRESH MATERIALIZED VIEW{concurrently} {db_name};'.format(
            concurrently=' CONCURRENTLY' if concurrently else '', db_name=self.db_name
        )


class ViewMeta(DeclarativeObjectMeta):
    @property
    def definition(cls):
        if cls.abstract:
            raise TypeError('{} is abstract, so it has no definition.'.format(cls.__name__))

        if not cls.sql:
            raise TypeError('{} declares no sql, so there is nothing to select from.'.format(cls.__name__))

        return ViewDefinition(
            name=cls.name,
            db_name=cls.resolved_db_name,
            sql=cls.sql,
            options=dict(cls.options or {}),
        )


class MaterializedViewMeta(ViewMeta):
    @property
    def definition(cls):
        if cls.abstract:
            raise TypeError('{} is abstract, so it has no definition.'.format(cls.__name__))

        if not cls.sql:
            raise TypeError('{} declares no sql, so there is nothing to select from.'.format(cls.__name__))

        return MaterializedViewDefinition(
            name=cls.name,
            db_name=cls.resolved_db_name,
            sql=cls.sql,
            options=dict(cls.options or {}),
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

    #: The SELECT the view is defined as. A trailing semicolon is optional.
    sql = None

    #: Reloptions, rendered into the CREATE's WITH clause, e.g. {'security_invoker': 'true'} or
    #: {'check_option': 'cascaded'}.
    options = None


class MaterializedView(View, metaclass=MaterializedViewMeta):
    """
    A declared PostgreSQL materialized view, whose rows are stored and refreshed rather than computed per query.

    :Example:
        .. code-block:: python

            from postgres_objects import MaterializedView


            class CakeTotals(MaterializedView):
                sql = 'SELECT baker_id, count(*) AS cakes FROM example_cake GROUP BY baker_id'
                unique_index = ('baker_id',)

    Populate it again with the ``RefreshMaterializedView`` operation, which is written by hand rather than autodetected:
    whether the stored rows are stale is not something a declaration can know.
    """

    abstract = True

    #: Columns of the unique index every concurrent refresh needs. Declared separately from `indexes` because that is
    #: the whole reason it matters.
    unique_index = None

    #: Further indexes, each a tuple of column names.
    indexes = ()

    #: Whether to populate the view as it is created. A large view is often cheaper to create empty and refresh later.
    with_data = True
