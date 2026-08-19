"""
The declaration/definition split every kind of managed database object is built on.

PostgreSQL objects that are not tables have no representation in Django's migration state, so this package manages them
as two halves:

* A **declaration** is a class, written in an app's declarations module the same way a model is written in models.py.
  Being a class is what lets a project annotate it with a decorator, and it is what makes the declaring module (and
  therefore the owning app) knowable without inspecting the call stack. A declaration is never instantiated.
* A **definition** is a frozen value object holding exactly the attributes that end up in SQL. Operations hold one, and
  migrations serialize one. Because a migration never refers back to the live declaration, editing a declaration cannot
  silently rewrite the history of what has already been applied.

This mirrors Django's own split between a model class and the ModelState the migration framework works with.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db.backends.utils import truncate_name

# Postgres caps identifiers at 63 bytes. Only the object's own name is subject to it, not the whole signature.
MAX_IDENTIFIER_LENGTH = 63


def freeze(value):
    """
    Make a deconstructed field value hashable: dicts become tuples of their sorted items, lists and tuples become
    tuples, both applied recursively. Equal values freeze equal, which is what lets a hash be built on the result.
    """
    if isinstance(value, dict):
        return tuple((key, freeze(item)) for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)

    return value


class Change:
    """
    How to get from one definition of an object to the next.

    The distinction that matters is whether the old and new object can coexist. When they can, the two steps straddle
    the model migrations, which gives anything depending on the old one (a generated column, say) a migration in between
    to move across. When they cannot, both steps have to happen together and every dependent has to already be off the
    object.

    Which side of the model migrations each step lands on is not decided here: it follows from the kind of object, via
    ObjectDefinition.precedes_models.
    """

    UNCHANGED = 'unchanged'
    ALTER = 'alter'
    #: Drop and recreate together, on the side the object is created on.
    REPLACE = 'replace'
    #: Create the new one on the object's own side, drop the old one on the other.
    SUPERSEDE = 'supersede'


class ObjectDefinition:
    """
    Base for the frozen value objects that operations hold and migrations serialize.
    """

    #: Attribute names carried by this kind of object, in the order deconstruct() writes them.
    fields = ()

    #: Whether the object has to exist before the model migrations of its app run.
    #:
    #: A function is called from a generated column's expression, so it is created before them and dropped after. A
    #: view reads from tables, so it is the inverse: created after them, and dropped before, since PostgreSQL
    #: refuses to alter a column a view depends on.
    precedes_models = True

    #: What this kind of object is called in an operation's description.
    object_noun = 'object'

    #: The family this object belongs to, used to key what the migrations have created.
    #:
    #: Deliberately the family and not the exact class: PostgreSQL keeps functions and relations in separate namespaces,
    #: so a function and a view may have identical identifiers and must not be conflated, while a plain view turning
    #: into a materialized one is the *same* object changing and has to be seen as a change rather than as an unrelated
    #: object appearing beside the old one.
    kind = None

    #: The operations that create, alter and remove this kind of object. Set on the subclass, which is why they are
    #: named rather than imported here: operations.py knows nothing about any particular kind of object.
    add_operation_class = None
    alter_operation_class = None
    remove_operation_class = None

    def __init__(self, **kwargs):
        missing = [field for field in self.fields if field not in kwargs]
        if missing:
            raise TypeError('{} is missing {}.'.format(type(self).__name__, ', '.join(missing)))

        unexpected = [key for key in kwargs if key not in self.fields]
        if unexpected:
            raise TypeError('{} got unexpected {}.'.format(type(self).__name__, ', '.join(sorted(unexpected))))

        for field in self.fields:
            setattr(self, field, kwargs[field])

    def __eq__(self, other):
        return type(self) is type(other) and self.deconstruct() == other.deconstruct()

    def __hash__(self):
        # Built over the same deconstruction __eq__ compares, so equal definitions hash equal.
        path, args, kwargs = self.deconstruct()
        return hash((path, freeze(args), freeze(kwargs)))

    def __repr__(self):
        return '<{}: {}>'.format(type(self).__name__, self.db_name)

    def deconstruct(self):
        path = '{}.{}'.format(type(self).__module__, type(self).__qualname__)
        return (path, [], {field: getattr(self, field) for field in self.fields})

    @property
    def description(self):
        """
        How this object is named in an operation's description. Overridden where a name alone is ambiguous.
        """
        return self.db_name

    def create_sql(self):
        raise NotImplementedError

    def drop_sql(self, schema_name):
        raise NotImplementedError

    def create_statements(self):
        """
        Every statement needed to create the object, in order.

        Most objects are one statement; a materialized view is its CREATE plus an index per declared index. Operations
        execute these one at a time rather than as a single blob, so sqlmigrate shows them separately.
        """
        return (self.create_sql(),)

    def drop_statements(self, schema_name):
        return (self.drop_sql(schema_name),)

    def plan_change_from(self, previous):
        """
        Return the Change describing how to move from `previous` to this definition.
        """
        raise NotImplementedError


class DeclarativeObjectMeta(type):
    """
    Metaclass for declarations.

    It resolves the object's name from the class name, refuses instantiation, and derives the owning app from the
    declaring module. Deriving the app from ``cls.__module__`` is the whole reason declarations are classes: an object
    built by a shared factory is still attributed to the module the factory writes into, and no stack inspection is
    involved.
    """

    def __new__(mcs, class_name, bases, namespace, **kwargs):
        cls = super().__new__(mcs, class_name, bases, namespace, **kwargs)

        # abstract is deliberately not inherited. Subclassing a declaration to reuse most of its body should give a
        # concrete object of its own, the way subclassing a concrete Django model does.
        cls.abstract = namespace.get('abstract', False)
        if not cls.abstract:
            cls.name = namespace.get('name') or class_name.lower()

        return cls

    def __call__(cls, *args, **kwargs):
        raise TypeError(
            '{} is a declaration, not a value, so it is never instantiated. Use {}.definition for the object the '
            'migration framework works with.'.format(cls.__name__, cls.__name__)
        )

    @property
    def resolved_app_label(cls):
        if cls.app_label:
            return cls.app_label

        app_config = apps.get_containing_app_config(cls.__module__)
        if app_config is None:
            raise ImproperlyConfigured(
                "Cannot tell which app '{}' belongs to, because it is not declared inside an installed app. Set "
                'app_label on it, or db_name to name it outright.'.format(cls.__name__)
            )

        return app_config.label

    @property
    def resolved_db_name(cls):
        if cls.db_name:
            return cls.db_name

        return truncate_name('{}_{}'.format(cls.resolved_app_label, cls.name), MAX_IDENTIFIER_LENGTH)

    @property
    def definition(cls):
        """
        Build the frozen definition for this declaration.
        """
        raise NotImplementedError


class DeclarativeObject(metaclass=DeclarativeObjectMeta):
    """
    Base class for every kind of declared database object.
    """

    abstract = True

    #: Hints handed to the database router's allow_migrate for every operation on this object.
    #:
    #: A project that routes its migrations somewhere other than one plain database annotates its declarations by
    #: setting this, normally through a decorator. Left empty, allow_migrate is consulted without hints and a project
    #: with no routers configured runs every operation on the default connection.
    router_hints = {}

    #: Overrides the name derived from the class name.
    name = None
    #: Overrides the app derived from the declaring module.
    app_label = None
    #: Overrides the identifier the object is created under, otherwise '{app_label}_{name}'.
    db_name = None
