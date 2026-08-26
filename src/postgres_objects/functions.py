"""
Declarative PostgreSQL functions.
"""

import re

from django.db.models import Func

from postgres_objects.base import Change, DeclarativeObject, DeclarativeObjectMeta, ObjectDefinition, quote_name
from postgres_objects.operations import AddFunction, AlterFunction, RemoveFunction

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


#: The parameter modes Postgres knows. IN is what an unmarked parameter means, and OUT parameters are not inputs, so
#: neither distinguishes one function from another; INOUT and VARIADIC do.
ARGUMENT_MODES = ('in', 'out', 'inout', 'variadic')


def normalize_significant(text):
    """
    Fold `text` the way Postgres reads it: everything outside quoting lowercased with whitespace runs collapsed to a
    single space, quoted spans kept verbatim since their case is significant.
    """
    parts = []
    position = 0

    for index, character, depth in significant_characters(text):
        if index > position:
            # A span the scanner skipped is quoted, and copied unchanged.
            parts.append(text[position:index])
        if character.isspace():
            if parts and parts[-1] != ' ':
                parts.append(' ')
        else:
            parts.append(character.lower())
        position = index + 1

    if position < len(text):
        parts.append(text[position:])

    return ''.join(parts).strip()


def split_mode(argument):
    """
    Split one normalized argument into its mode and the rest, reading an unmarked parameter as IN.
    """
    token, _, tail = argument.partition(' ')
    if tail and token in ARGUMENT_MODES:
        return token, tail

    return 'in', argument


def identity_arguments(arguments):
    """
    The normalized (mode, argument) pairs Postgres identifies the function by: defaults stripped, case and whitespace
    folded, and OUT parameters left out entirely, since only inputs make an overload.
    """
    pairs = []
    for argument in split_arguments(arguments):
        mode, rest = split_mode(normalize_significant(strip_default_clause(argument)))
        if mode != 'out':
            pairs.append((mode, rest))

    return pairs


def arguments_match(ours, theirs):
    """
    Whether two identity argument pairs can be the same parameter.

    Parameter names are optional and types can be several words long, so the two cannot be told apart reliably.
    Instead each side may shed one leading token (a candidate name) and the tails are compared. The bias is deliberate:
    a false match degrades to an in-place change that at worst fails loudly, while a false mismatch plans the
    superseding shape whose trailing drop silently destroys the function.
    """
    if ours[0] != theirs[0]:
        return False

    def variants(rest):
        tail = rest.partition(' ')[2]
        return {rest, tail} if tail else {rest}

    return bool(variants(ours[1]) & variants(theirs[1]))


class FunctionDefinition(ObjectDefinition):
    """
    Everything about a function that ends up in SQL. This is what migrations serialize.
    """

    fields = ('name', 'db_name', 'arguments', 'returns', 'body', 'language', 'volatility', 'strict', 'parallel')

    kind = 'function'
    object_noun = 'function'
    add_operation_class = AddFunction
    alter_operation_class = AlterFunction
    remove_operation_class = RemoveFunction

    @property
    def description(self):
        # The signature rather than the name: overloads share a name and only the arguments tell them apart.
        return self.signature

    @property
    def signature(self):
        return '{}({})'.format(self.db_name, self.arguments)

    @property
    def drop_arguments(self):
        """
        The argument list DROP FUNCTION identifies the function by: every parameter default clause stripped.
        """
        return ', '.join(strip_default_clause(argument) for argument in split_arguments(self.arguments))

    @property
    def drop_signature(self):
        """
        Signature specifically built for DROP FUNCTION (without any parameter default clauses). Bare like `signature`:
        both are identity and description strings, and only rendered SQL quotes the name.
        """
        return '{}({})'.format(self.db_name, self.drop_arguments)

    def create_sql(self):
        modifiers = [self.volatility]
        if self.strict:
            modifiers.append('STRICT')
        modifiers.append('PARALLEL {}'.format(self.parallel))

        # Only the name is an identifier to quote; the argument list is Postgres syntax and stays verbatim.
        return FUNCTION_CREATE_SQL.format(
            signature='{}({})'.format(quote_name(self.db_name), self.arguments),
            returns=self.returns,
            body=self.body.strip(),
            language=self.language,
            modifiers=' '.join(modifiers),
        )

    def drop_sql(self, schema_name):
        return 'DROP FUNCTION IF EXISTS "{schema}".{name}({arguments});'.format(
            schema=schema_name, name=quote_name(self.db_name), arguments=self.drop_arguments
        )

    def is_same_identity(self, previous):
        """
        Whether Postgres would consider this and the previous definition the same function: the same name and the same
        input argument types, everything the identity ignores (defaults, parameter names, case, whitespace, etc.) folded
        away. Type spellings are not normalized (INT and INTEGER read as different), which errs toward the overload
        plan; see arguments_match for why the remaining bias points the other way.
        """
        if normalize_significant(self.db_name) != normalize_significant(previous.db_name):
            return False

        ours, theirs = identity_arguments(self.arguments), identity_arguments(previous.arguments)
        return len(ours) == len(theirs) and all(arguments_match(a, b) for a, b in zip(ours, theirs))

    def plan_change_from(self, previous):
        if previous == self:
            return Change.UNCHANGED

        if not self.is_same_identity(previous):
            # A new identity coexists with the old one as an overload, so the replacement can be created up front and
            # the old copy dropped only after the model migrations have moved every dependent onto it.
            return Change.SUPERSEDE

        if normalize_significant(previous.returns) != normalize_significant(self.returns) or identity_arguments(
            previous.arguments
        ) != identity_arguments(self.arguments):
            # CREATE OR REPLACE refuses a return-type change and a parameter rename, and with the identity unchanged
            # there is no overload to hide behind, so the old function has to be removed first. A dependent object
            # (e.g. a generated column) blocks this drop on purpose: it has to be migrated off the function before the
            # change can happen. Crucially this is never the superseding shape: its trailing drop would resolve to the
            # very function the leading create just wrote, silently deleting it.
            return Change.REPLACE

        return Change.ALTER

    def alters_computed_values_from(self, previous):
        """
        Whether values this function computed under the previous definition may now come out differently.

        The body is the obvious carrier, the language decides what the body text means, and STRICT changes what NULL
        input produces. Volatility and parallel safety are promises to the planner; they cannot change a result, so a
        change to them alone is no reason to recompute anything.
        """
        return (self.body, self.language, self.strict) != (previous.body, previous.language, previous.strict)


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

    def check_declaration(cls):
        """
        Refuse a declaration that cannot produce working SQL, before a migration is written for it.

        Without this, a forgotten returns is interpolated as the literal RETURNS None into the CREATE statement of a
        migration makemigrations happily writes, and a forgotten body surfaces only at migrate time, as an
        AttributeError naming nothing.
        """
        if cls.abstract:
            raise TypeError('{} is abstract, so it has no definition.'.format(cls.__name__))

        if not cls.returns:
            raise TypeError(
                '{} declares no returns, so there is no type for CREATE FUNCTION to return.'.format(cls.__name__)
            )

        if not cls.body:
            raise TypeError(
                '{} declares no body, so there is no function for CREATE FUNCTION to define.'.format(cls.__name__)
            )

    @property
    def definition(cls):
        cls.check_declaration()

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

    #: The argument list, verbatim, as PostgreSQL spells it. Defaults to no arguments.
    arguments = ''

    #: The return type. Required on a concrete declaration.
    returns = None

    #: The function body. Emitted verbatim, so a literal ``%`` needs no escaping. Required on a concrete declaration.
    body = None

    #: The language the body is written in.
    language = 'plpgsql'

    #: ``'VOLATILE'``, ``'STABLE'`` or ``'IMMUTABLE'``.
    volatility = 'VOLATILE'

    #: Whether to emit ``STRICT``, so the function returns NULL as soon as any argument is NULL.
    strict = False

    #: ``'UNSAFE'``, ``'RESTRICTED'`` or ``'SAFE'``.
    parallel = 'UNSAFE'

    #: Result type for annotations, so it does not have to be repeated at every call site.
    output_field = None
