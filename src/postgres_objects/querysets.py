"""
Turning a declared queryset into the two things the package needs from it: the SELECT a migration can carry, and the
columns that SELECT produces.

Everything here runs without touching the database. Compiling asks the connection only for its dialect, and quoting goes
through psycopg's global adaptation rules, which is also why a project registering custom adapters per connection could
in principle see a different literal than a live cursor would produce.
"""

from importlib import import_module
from operator import attrgetter

import django
from django.apps.registry import Apps
from django.core.exceptions import EmptyResultSet, FieldError
from django.db import DJANGO_VERSION_PICKLE_KEY, connections, models
from django.db.models import QuerySet
from django.db.models.sql import Query

#: Auto fields describe a column a table fills in on insert, which is not something a view has. They also come with a
#: primary_key=True their deconstruction puts back however it is cleared, and a model may carry only one of them, so a
#: queryset selecting two of them (a pk and a foreign key, say) could not be modelled at all. The plain integer
#: equivalent says the same thing about the column without any of that.
AUTO_FIELD_EQUIVALENTS = {
    'AutoField': models.IntegerField,
    'BigAutoField': models.BigIntegerField,
    'SmallAutoField': models.SmallIntegerField,
}


class CompiledQueryset:
    """
    What a declared queryset compiles to: the SELECT, the columns it produces, in order, and the tables it reads.
    """

    def __init__(self, sql, columns, tables):
        self.sql = sql
        self.columns = columns
        self.tables = tables

    @property
    def column_names(self):
        return tuple(name for name, _ in self.columns)


def walk_expressions(node):
    """
    A node and everything below it, following the source expressions each one exposes.

    Both WhereNode and every expression answer get_source_expressions(), which is what makes one walk cover a filter
    tree and an annotation alike. Anything that answers neither is a leaf.
    """
    yield node

    get_source_expressions = getattr(node, 'get_source_expressions', None)
    if get_source_expressions is None:
        return

    for child in get_source_expressions():
        if child is not None:
            yield from walk_expressions(child)


def referenced_tables(query):
    """
    Every table the query reads, including through its subqueries.

    The joined tables are the alias map; a subquery has an alias map of its own and is reached through the expressions
    holding it (a Subquery annotation, an __in over a queryset, or an Exists in a filter).

    This deliberately fails open. A construct the walk does not reach contributes no reference, which is no worse than
    the ordering this package had before it read any of this, and `depends_on` is how a declaration says what was
    missed.
    """
    tables = set()
    pending = [query]
    seen = set()

    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue

        seen.add(id(current))

        for alias in current.alias_map.values():
            tables.add(alias.table_name)

        roots = (current.where, *current.annotations.values(), *(current.combined_queries or ()))
        for root in roots:
            for expression in walk_expressions(root):
                if isinstance(expression, Query):
                    pending.append(expression)

    return tables


def column_name(expression, alias):
    """
    What the view will call a given column.

    The alias is authoritative when there is one. A bare model column has none and inherits the name of the column it
    reads. Anything else has no single name to give, and is refused rather than guessed at.
    """
    if alias:
        return alias

    target = getattr(expression, 'target', None)
    if target is None:
        raise TypeError(
            'Cannot tell what {!r} would be called in the view. Select it through values() or annotate() so it is '
            'named.'.format(expression)
        )

    return target.column


def column_field(expression):
    """
    An unbound field describing this column, ready to be named by the model that takes it.
    """
    field = expression.output_field

    # A generated column's field carries the expression that computes it, which only means anything on the model it was
    # declared on. What the view holds is the computed value, so what describes it is the output type.
    while isinstance(field, models.GeneratedField):
        field = field.output_field

    # A relation would carry a reference to a model the private registry knows nothing about, and never resolve. The
    # view holds the raw key, so the column is typed as the key it points at. Followed to the end of the chain: a key
    # pointing at a multi-table-inheritance child lands on the parent link, which is itself a relation.
    while field.is_relation:
        field = field.target_field

    equivalent = AUTO_FIELD_EQUIVALENTS.get(field.get_internal_type())
    field = equivalent() if equivalent else field.clone()

    # clone() rebuilds the field from its deconstruction, so it arrives unbound. What survives is the placement, and
    # none of that describes the view's column: db_column above all, because a values() column is named after the field
    # while the view's column is named after the alias.
    field.db_column = None
    field.primary_key = False
    field.unique = False
    field.db_index = False

    return field


def inline_params(sql, params, connection):
    """
    Fold the query's parameters into it as literals.

    A view carries no parameters: whatever the queryset filtered on has to be written into the CREATE. This is the same
    interpolation sqlmigrate does.
    """
    # Deliberately not entered as a context manager: __enter__ opens a transaction, and therefore a connection, while
    # all that is wanted here is quote_value, which adapts through psycopg's global rules and needs no connection.
    schema_editor = connection.schema_editor(collect_sql=True)

    try:
        return sql % tuple(schema_editor.quote_value(param) for param in params)
    except (TypeError, ValueError) as error:
        raise ValueError('The queryset parameters could not be written as literals: {}'.format(error)) from error


def compile_queryset(queryset):
    """
    Compile a declared queryset into the SELECT the view is defined as, the columns it produces, and the tables it
    reads.

    The connection is the one the queryset itself reads from (the router's db_for_read answer) but it only decides the
    dialect and the quoting. The SQL that comes out is frozen into a migration, and every environment applying that
    migration gets the same string.
    """
    if not isinstance(queryset, QuerySet):
        if hasattr(queryset, 'get_queryset'):
            raise TypeError('queryset() returned a manager rather than a queryset. Add .all() to what it returns.')

        raise TypeError('queryset() must return a queryset, not {!r}.'.format(queryset))

    connection = connections[queryset.db]
    if connection.vendor != 'postgresql':
        raise TypeError(
            "The '{}' connection the queryset reads from is not PostgreSQL, so its SQL would not make a PostgreSQL "
            'view.'.format(queryset.db)
        )

    if queryset.query.combinator:
        raise TypeError(
            'The queryset is a {}, whose columns Django names col1, col2 and so on. Write the sql for this view by '
            'hand instead.'.format(queryset.query.combinator)
        )

    # A clone, so compiling never mutates a queryset the declaration might reuse.
    query = queryset.all().query
    compiler = query.get_compiler(using=queryset.db)

    try:
        # as_sql() runs pre_sql_setup itself and leaves compiler.select populated for the column walk below.
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        raise TypeError('The queryset selects nothing, so there is no view to create.') from None

    columns = []
    seen = set()
    for expression, _, alias in compiler.select:
        name = column_name(expression, alias)

        if name in seen:
            raise TypeError(
                "The queryset selects two columns named '{}', which a view cannot hold. Name one of them through "
                'values() or annotate().'.format(name)
            )

        seen.add(name)

        try:
            columns.append((name, column_field(expression)))
        except FieldError as error:
            raise TypeError("The type of column '{}' is not knowable: {}".format(name, error)) from error

    # Read after as_sql(), which is what populates the alias map of a subquery that had not been resolved yet.
    tables = tuple(sorted(referenced_tables(query)))

    return CompiledQueryset(inline_params(sql, params, connection), tuple(columns), tables)


def resolve_primary_key(declaration, column_names):
    """
    Which column the generated model is keyed by.

    Django insists on a primary key, and letting it invent one is not an option: a model without one gets an implicit id
    column added, which the view does not have, and every query would then ask the database for it. Our rule is:
    explicit first, then what a materialized view already had to say, then the conventional id, and if none of those
    provide an answer it is an error.
    """
    if declaration.primary_key:
        column = declaration.primary_key
    elif len(getattr(declaration, 'unique_index', None) or ()) == 1:
        column = declaration.unique_index[0]
    elif 'id' in column_names:
        column = 'id'
    else:
        raise TypeError(
            'Cannot tell which column of {} identifies a row, so it has no model. Set primary_key on it to name one '
            'of {}.'.format(declaration.__name__, ', '.join(column_names))
        )

    if column not in column_names:
        raise TypeError(
            "{} names '{}' as its primary key, but its queryset selects {}.".format(
                declaration.__name__, column, ', '.join(column_names)
            )
        )

    return column


def unpickle_view_row(module_path, qualname):
    """
    Rebuild an empty row of a generated model by finding its declaration again.

    Django's own unpickling asks the app registry for the model, and the generated model is deliberately not in it, so a
    pickled row records the declaration's import path instead.
    """
    declaration = attrgetter(qualname)(import_module(module_path))
    model = declaration.model

    return model.__new__(model)


def build_model(declaration, compiled):
    """
    Build an unmanaged model over the columns of a view.

    It is registered in a private app registry rather than the project's. A model in the project's registry is part of
    the migration state Django's autodetector compares, so it would be detected as a table to create, registered as a
    content type and come with automatic permissions. Our private registry keeps the model queryable while invisible.
    """
    primary_key = resolve_primary_key(declaration, compiled.column_names)

    def __reduce__(self):
        # The version stamp is what Model.__reduce__ adds too; Model.__setstate__ warns when it is missing.
        state = self.__getstate__()
        state[DJANGO_VERSION_PICKLE_KEY] = django.__version__

        return (unpickle_view_row, (declaration.__module__, declaration.__qualname__), state)

    attributes = {
        '__module__': declaration.__module__,
        # The declaration keeps its own qualified name; this says in a traceback which of the two is being looked at.
        '__qualname__': '{}.model'.format(declaration.__qualname__),
        '__doc__': 'The model generated from the {} view declaration.'.format(declaration.__name__),
        '__reduce__': __reduce__,
        'Meta': type(
            'Meta',
            (),
            {
                'apps': Apps(installed_apps=[]),
                'app_label': declaration.resolved_app_label,
                'db_table': declaration.resolved_db_name,
                'managed': False,
            },
        ),
    }

    for name, field in compiled.columns:
        if name in attributes or name in ('objects', 'pk') or hasattr(models.Model, name):
            raise TypeError(
                "The queryset selects a column named '{}', which every Django model already claims. Rename it "
                'through values() or annotate().'.format(name)
            )

        if name == primary_key:
            field.primary_key = True

        attributes[name] = field

    return type(declaration.__name__, (models.Model,), attributes)
