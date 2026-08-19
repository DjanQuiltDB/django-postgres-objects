"""
Declarative PostgreSQL functions.
"""

import re

from django.db.models import Func

from postgres_objects.base import Change, DeclarativeObject, DeclarativeObjectMeta, ObjectDefinition

FUNCTION_CREATE_SQL = """
CREATE OR REPLACE FUNCTION {signature} RETURNS {returns} AS $function$
{body}
$function$ LANGUAGE {language} {modifiers};
"""

#: The word DEFAULT starting a default clause, checked at a position the scanner already knows is outside all quoting.
DEFAULT_KEYWORD = re.compile(r'DEFAULT\s', re.IGNORECASE)
#: A dollar-quote opener: $$ or $tag$, matched at a $. A bare $ (a positional parameter, say) matches neither.
DOLLAR_QUOTE_TAG = re.compile(r'\$(?:[A-Za-z_][0-9A-Za-z_]*)?\$')
#: A string opener whose standalone E prefix makes backslashes escape the next character.
E_STRING_OPENER = re.compile(r"(?<![0-9A-Za-z_$])[Ee]'")


def significant_characters(text):
    """
    Yield (index, character, depth) for every character of `text` outside all quoting.

    PostgreSQL argument lists quote in four ways: '...' strings (where '' doubles a quote, which scans the same as the
    string closing and reopening), E'...' strings where a backslash escapes the next character, "..." identifiers, and
    $tag$...$tag$ dollar quoting. Only a character outside all of them can split arguments or start a DEFAULT clause,
    and one inside parentheses or brackets (depth above zero) cannot either.
    """
    depth = 0
    index = 0
    length = len(text)

    while index < length:
        character = text[index]

        if character == "'":
            escaping = index > 0 and E_STRING_OPENER.match(text, index - 1) is not None
            index += 1
            while index < length and text[index] != "'":
                index += 2 if escaping and text[index] == '\\' else 1
            index += 1
            continue

        if character == '"':
            closer = text.find('"', index + 1)
            index = length if closer < 0 else closer + 1
            continue

        if character == '$':
            tag = DOLLAR_QUOTE_TAG.match(text, index)
            if tag:
                closer = text.find(tag.group(), tag.end())
                index = length if closer < 0 else closer + len(tag.group())
                continue

        if character in '([':
            depth += 1
        elif character in ')]':
            depth -= 1

        yield index, character, depth
        index += 1


def split_arguments(arguments):
    """
    Split a PostgreSQL argument list on its top-level commas.
    """
    parts = []
    start = 0

    for index, character, depth in significant_characters(arguments):
        if character == ',' and depth == 0:
            parts.append(arguments[start:index])
            start = index + 1

    parts.append(arguments[start:])
    return [part.strip() for part in parts if part.strip()]


def strip_default_clause(argument):
    """
    Cut the default clause (either spelling) off one argument, leaving the part DROP FUNCTION identifies it by.
    """
    for index, character, depth in significant_characters(argument):
        if depth:
            continue
        if character == '=' or (
            character in 'Dd' and index and argument[index - 1].isspace() and DEFAULT_KEYWORD.match(argument, index)
        ):
            return argument[:index].rstrip()

    return argument


class FunctionDefinition(ObjectDefinition):
    """
    Everything about a function that ends up in SQL. This is what migrations serialize.
    """

    fields = ('name', 'db_name', 'arguments', 'returns', 'body', 'language', 'volatility', 'strict', 'parallel')

    @property
    def signature(self):
        return '{}({})'.format(self.db_name, self.arguments)

    @property
    def drop_signature(self):
        """
        Signature specifically built for DROP FUNCTION (without any parameter default clauses).
        """
        arguments = ', '.join(strip_default_clause(argument) for argument in split_arguments(self.arguments))
        return '{}({})'.format(self.db_name, arguments)

    def create_sql(self):
        modifiers = [self.volatility]
        if self.strict:
            modifiers.append('STRICT')
        modifiers.append('PARALLEL {}'.format(self.parallel))

        return FUNCTION_CREATE_SQL.format(
            signature=self.signature,
            returns=self.returns,
            body=self.body.strip(),
            language=self.language,
            modifiers=' '.join(modifiers),
        )

    def drop_sql(self, schema_name):
        return 'DROP FUNCTION IF EXISTS "{schema}".{signature};'.format(
            schema=schema_name, signature=self.drop_signature
        )

    def plan_change_from(self, previous):
        if previous == self:
            return Change.UNCHANGED

        if previous.signature == self.signature and previous.returns != self.returns:
            # CREATE OR REPLACE refuses a return-type change, and with an identical signature there is no overload to
            # hide behind, so the old function has to be removed first. A dependent object (e.g. a generated column)
            # blocks this drop on purpose: it has to be migrated off the function before the type can change.
            return Change.REPLACE

        if previous.signature != self.signature:
            # A new signature coexists with the old one as an overload, so the replacement can be created up front and
            # the old copy dropped only after the model migrations have moved every dependent onto it.
            return Change.SUPERSEDE

        return Change.ALTER


class FunctionMeta(DeclarativeObjectMeta):
    def __call__(cls, *expressions, **extra):
        """
        Build a queryset expression calling this function.

        The function is named unqualified on purpose. It may live in a different schema than the one the connection is
        pointed at while still being on the search path, and qualifying it here would pin it to whichever schema
        happened to build the query.
        """
        if cls.abstract:
            raise TypeError('{} is abstract, so it names no function to call.'.format(cls.__name__))

        extra.setdefault('output_field', cls.output_field)
        return Func(*expressions, function=cls.resolved_db_name, **extra)

    @property
    def definition(cls):
        if cls.abstract:
            raise TypeError('{} is abstract, so it has no definition.'.format(cls.__name__))

        return FunctionDefinition(
            name=cls.name,
            db_name=cls.resolved_db_name,
            arguments=cls.arguments,
            returns=cls.returns,
            body=cls.body,
            language=cls.language,
            volatility=cls.volatility,
            strict=cls.strict,
            parallel=cls.parallel,
        )


class Function(DeclarativeObject, metaclass=FunctionMeta):
    """
    A declared PostgreSQL function, managed as migration operations instead of raw SQL.

    :Example:
        .. code-block:: python

            from postgres_objects import Function


            class AllUppercase(Function):
                arguments = 'input TEXT'
                returns = 'TEXT'
                volatility = 'IMMUTABLE'
                strict = True
                parallel = 'SAFE'
                body = '''
                    BEGIN
                        RETURN UPPER(input);
                    END;
                '''

    The class is callable as a queryset expression, so the same declaration serves both the migration that creates the
    function and the queries that call it::

        Cake.objects.annotate(uppercased=AllUppercase(F('name')))
    """

    abstract = True

    arguments = ''
    returns = None
    body = None
    language = 'plpgsql'
    volatility = 'VOLATILE'
    strict = False
    parallel = 'UNSAFE'

    #: Result type for annotations, so it does not have to be repeated at every call site.
    output_field = None
