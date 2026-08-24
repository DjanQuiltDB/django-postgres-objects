from django.test import SimpleTestCase

from postgres_objects import Function
from postgres_objects.base import Change
from postgres_objects.functions import FunctionDefinition, split_arguments


def declare(class_name, **attrs):
    """
    Build a declaration on the fly, so a test can vary one attribute without a fixture module per case.
    """
    namespace = {
        'app_label': 'example',
        'arguments': 'input TEXT',
        'returns': 'TEXT',
        'body': """
            BEGIN
                RETURN input;
            END;
        """,
    }
    namespace.update(attrs)

    return type(Function)(class_name, (Function,), namespace)


class SplitArgumentsTestCase(SimpleTestCase):
    def test_it_splits_on_top_level_commas(self):
        """
        Case: A plain argument list.
        Expected: One part per argument, whitespace trimmed.
        """
        self.assertEqual(split_arguments('a TEXT, b INT'), ['a TEXT', 'b INT'])

    def test_it_ignores_commas_inside_parentheses(self):
        """
        Case: An argument whose type carries a precision, so it contains a comma of its own.
        Expected: That comma does not split the argument in two.
        """
        self.assertEqual(split_arguments('a NUMERIC(10, 2), b INT'), ['a NUMERIC(10, 2)', 'b INT'])

    def test_it_ignores_commas_inside_quotes(self):
        """
        Case: A default value that is a string literal containing a comma.
        Expected: The literal is left whole.
        """
        self.assertEqual(split_arguments("a TEXT DEFAULT 'x,y', b INT"), ["a TEXT DEFAULT 'x,y'", 'b INT'])

    def test_it_ignores_commas_inside_dollar_quotes(self):
        """
        Case: A default value written as a dollar-quoted string containing a comma.
        Expected: The literal is left whole, since dollar quoting is quoting like any other.
        """
        self.assertEqual(
            split_arguments('greeting TEXT DEFAULT $$Hello, world$$, flags INT DEFAULT 0'),
            ['greeting TEXT DEFAULT $$Hello, world$$', 'flags INT DEFAULT 0'],
        )

    def test_a_backslash_inside_an_e_string_does_not_end_it(self):
        """
        Case: An E-string default whose content is an escaped quote.
        Expected: The backslash does not flip the quote state, so the argument after the literal survives as its own
                  part.
        """
        self.assertEqual(
            split_arguments("pattern TEXT DEFAULT E'\\'', flags INT DEFAULT 0"),
            ["pattern TEXT DEFAULT E'\\''", 'flags INT DEFAULT 0'],
        )

    def test_an_empty_argument_list_splits_to_nothing(self):
        """
        Case: A function taking no arguments.
        Expected: No parts, rather than one empty one that would produce a stray comma downstream.
        """
        self.assertEqual(split_arguments(''), [])


class SignatureTestCase(SimpleTestCase):
    def test_the_signature_names_the_db_name(self):
        """
        Case: A declaration whose Python name and database identifier differ.
        Expected: The signature names the database identifier, since that is what CREATE FUNCTION needs.
        """
        self.assertEqual(declare('Doubled').definition.signature, 'example_doubled(input TEXT)')

    def test_the_drop_signature_strips_a_default_clause(self):
        """
        Case: A function with a parameter default.
        Expected: DROP FUNCTION names the types only, since it refuses a default clause.
        """
        definition = declare('WithDefault', arguments="input TEXT, suffix TEXT DEFAULT '!'").definition

        self.assertEqual(definition.drop_signature, 'example_withdefault(input TEXT, suffix TEXT)')

    def test_the_drop_signature_strips_an_equals_default(self):
        """
        Case: A parameter default written with = rather than the DEFAULT keyword.
        Expected: Stripped just the same, since Postgres accepts both spellings.
        """
        definition = declare('WithEquals', arguments="input TEXT, suffix TEXT = '!'").definition

        self.assertEqual(definition.drop_signature, 'example_withequals(input TEXT, suffix TEXT)')

    def test_the_drop_signature_survives_a_dollar_quoted_default(self):
        """
        Case: A parameter default dollar-quoted around a comma, followed by another parameter.
        Expected: Both parameters reach the drop signature with their defaults stripped whole, rather than the literal
                  being split at its comma.
        """
        definition = declare(
            'WithDollar', arguments='greeting TEXT DEFAULT $$Hello, world$$, flags INT DEFAULT 0'
        ).definition

        self.assertEqual(definition.drop_signature, 'example_withdollar(greeting TEXT, flags INT)')

    def test_the_drop_signature_survives_an_e_string_default(self):
        """
        Case: A parameter default written as an E-string escaping a quote, followed by another parameter.
        Expected: Both parameters reach the drop signature, rather than the second being swallowed by the misread
                  quote state.
        """
        definition = declare('WithEscape', arguments="pattern TEXT DEFAULT E'\\'', flags INT DEFAULT 0").definition

        self.assertEqual(definition.drop_signature, 'example_withescape(pattern TEXT, flags INT)')

    def test_the_drop_signature_keeps_an_out_parameter(self):
        """
        Case: A function with an OUT parameter.
        Expected: The mode is kept, because it is part of what identifies the function to DROP.
        """
        definition = declare('WithOut', arguments='input INT, OUT result INT').definition

        self.assertEqual(definition.drop_signature, 'example_without(input INT, OUT result INT)')


class CreateSqlTestCase(SimpleTestCase):
    def test_the_modifiers_follow_the_declaration(self):
        """
        Case: A declaration marked immutable, strict and parallel safe.
        Expected: All three reach the CREATE statement, which is what a generated column needs to be allowed.
        """
        definition = declare('Strictly', volatility='IMMUTABLE', strict=True, parallel='SAFE').definition

        self.assertIn('LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE', definition.create_sql())

    def test_a_non_strict_function_says_nothing_about_strictness(self):
        """
        Case: A declaration left at the defaults.
        Expected: STRICT is omitted rather than negated, since Postgres has no NOT STRICT to emit.
        """
        definition = declare('Loosely').definition

        self.assertIn('LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE', definition.create_sql())
        self.assertNotIn('STRICT', definition.create_sql())

    def test_the_body_is_emitted_verbatim(self):
        """
        Case: A body containing a comment.
        Expected: It survives into the CREATE statement unaltered.
        """
        definition = declare('Commented', body='BEGIN\n-- 100% of it\nRETURN input;\nEND;').definition

        self.assertIn('-- 100% of it', definition.create_sql())

    def test_the_drop_is_schema_qualified(self):
        """
        Case: Build the DROP for a named schema.
        Expected: The schema is quoted and named outright, so the drop cannot resolve through the search path.
        """
        definition = declare('Doubled').definition

        self.assertEqual(
            definition.drop_sql('some_schema'),
            'DROP FUNCTION IF EXISTS "some_schema".example_doubled(input TEXT);',
        )


class DefinitionValueTestCase(SimpleTestCase):
    def test_two_definitions_of_the_same_declaration_are_equal(self):
        """
        Case: Build the definition of the same declaration twice.
        Expected: Equal, so the autodetector can compare by value rather than identity.
        """
        self.assertEqual(declare('Doubled').definition, declare('Doubled').definition)

    def test_a_changed_body_makes_them_unequal(self):
        """
        Case: Two declarations differing only in their body.
        Expected: Unequal, which is what lets makemigrations notice an edited function.
        """
        self.assertNotEqual(declare('Doubled').definition, declare('Doubled', body='BEGIN END;').definition)

    def test_deconstruct_round_trips(self):
        """
        Case: Deconstruct a definition and rebuild it from the result.
        Expected: An equal definition, which is what makes a written migration reproduce the object.
        """
        definition = declare('Doubled').definition
        path, args, kwargs = definition.deconstruct()

        self.assertEqual(path, 'postgres_objects.functions.FunctionDefinition')
        self.assertEqual(FunctionDefinition(*args, **kwargs), definition)

    def test_equal_definitions_hash_equal(self):
        """
        Case: Hash the definitions of the same declaration built twice.
        Expected: Hashable at all, and hashing equal, which is what the hash contract demands of values that compare
                  equal.
        """
        self.assertEqual(hash(declare('Doubled').definition), hash(declare('Doubled').definition))

    def test_a_definition_works_as_a_set_member_and_dict_key(self):
        """
        Case: Put a definition in a set and use it as a dict key, then look both up with an equal definition.
        Expected: Found, so callers can deduplicate and index definitions the way any value object allows.
        """
        definition = declare('Doubled').definition

        self.assertIn(declare('Doubled').definition, {definition})
        self.assertEqual({definition: 'found'}[declare('Doubled').definition], 'found')

    def test_definitions_differing_in_one_field_are_two_set_members(self):
        """
        Case: Two definitions differing only in their body, collected into a set.
        Expected: Two members, since unequal definitions must not collapse into one.
        """
        members = {declare('Doubled').definition, declare('Doubled', body='BEGIN END;').definition}

        self.assertEqual(len(members), 2)

    def test_it_reprs_as_its_database_name(self):
        """
        Case: Print a definition, as a failing assertion or a debugger would.
        Expected: The identifier it is created under, which is what identifies it in the database.
        """
        self.assertEqual(repr(declare('Doubled').definition), '<FunctionDefinition: example_doubled>')

    def test_a_missing_field_is_refused(self):
        """
        Case: Build a definition without all of its fields.
        Expected: TypeError, rather than a definition that silently produces incomplete SQL.
        """
        with self.assertRaises(TypeError):
            FunctionDefinition(name='x')

    def test_an_unexpected_field_is_refused(self):
        """
        Case: An old migration passing a field this kind of object no longer carries.
        Expected: TypeError naming the problem, rather than the value being quietly ignored.
        """
        path, args, kwargs = declare('Doubled').definition.deconstruct()
        kwargs['foobar'] = 'nonsense'

        with self.assertRaises(TypeError):
            FunctionDefinition(*args, **kwargs)


class PlanChangeTestCase(SimpleTestCase):
    def test_an_identical_definition_is_unchanged(self):
        """
        Case: Compare a definition against an identical one.
        Expected: UNCHANGED, so a makemigrations run with no edits writes nothing.
        """
        previous = declare('Doubled').definition

        self.assertIs(declare('Doubled').definition.plan_change_from(previous), Change.UNCHANGED)

    def test_a_changed_body_is_an_alter(self):
        """
        Case: Same signature, same return type, different body.
        Expected: CREATE OR REPLACE handles it in place.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', body='BEGIN RETURN input || input; END;').definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_changed_return_type_is_a_replace(self):
        """
        Case: Same signature, different return type.
        Expected: The old function has to be dropped first, since CREATE OR REPLACE refuses this and there is no
                  overload to hide behind.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', returns='INT').definition

        self.assertIs(current.plan_change_from(previous), Change.REPLACE)

    def test_a_changed_signature_supersedes(self):
        """
        Case: Different argument list.
        Expected: The two coexist as overloads, so the old one is dropped only after dependents have moved across.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='input TEXT, suffix TEXT').definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_a_changed_signature_and_return_type_supersedes(self):
        """
        Case: Both the arguments and the return type changed.
        Expected: The overload rule wins, since a new signature can be created alongside the old one.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='input INT', returns='INT').definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_an_added_default_clause_is_an_alter(self):
        """
        Case: The same parameter with a default clause added.
        Expected: ALTER. Postgres identifies a function by its input types, so this is still the same function, and a
                  superseding trailing drop would destroy what the leading create just wrote.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments="input TEXT DEFAULT ''").definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_whitespace_only_argument_change_is_an_alter(self):
        """
        Case: The argument list reformatted, changing nothing Postgres can see.
        Expected: ALTER, not a supersede whose trailing drop would resolve to the function itself.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='input   TEXT').definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_case_only_argument_change_is_an_alter(self):
        """
        Case: The argument list recased, which Postgres folds to the same identifiers and types.
        Expected: ALTER, since the identity is unchanged.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='INPUT text').definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_spelled_out_in_mode_is_an_alter(self):
        """
        Case: The default IN mode written out explicitly.
        Expected: ALTER. IN is what an unmarked parameter already means.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='IN input TEXT').definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_renamed_parameter_is_a_replace(self):
        """
        Case: A parameter renamed, its type untouched.
        Expected: REPLACE. The identity is unchanged so a supersede would drop the new copy, and CREATE OR REPLACE
                  refuses to change a parameter name, so the old function has to be dropped first.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='source TEXT').definition

        self.assertIs(current.plan_change_from(previous), Change.REPLACE)

    def test_a_name_added_to_an_unnamed_parameter_is_a_replace(self):
        """
        Case: A previously unnamed parameter gains a name.
        Expected: REPLACE, treated like any other parameter naming change rather than risking the supersede shape.
        """
        previous = declare('Doubled', arguments='TEXT').definition
        current = declare('Doubled').definition

        self.assertIs(current.plan_change_from(previous), Change.REPLACE)

    def test_a_cosmetic_argument_change_with_a_new_return_type_is_a_replace(self):
        """
        Case: A default clause added and the return type changed in the same edit.
        Expected: REPLACE. The identity is unchanged so the return-type rule applies, where the raw signature comparison
                  used to fall through to the destructive supersede.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments="input TEXT DEFAULT ''", returns='INT').definition

        self.assertIs(current.plan_change_from(previous), Change.REPLACE)

    def test_a_case_only_return_type_change_is_an_alter(self):
        """
        Case: The return type recased.
        Expected: ALTER; text and TEXT are the same type, so there is nothing CREATE OR REPLACE would refuse.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', returns='text').definition

        self.assertIs(current.plan_change_from(previous), Change.ALTER)

    def test_a_changed_parameter_type_of_the_same_count_supersedes(self):
        """
        Case: One parameter's type changed, the argument count untouched.
        Expected: SUPERSEDE. A different input type is a genuine overload, guarding the identity heuristic against
                  over-matching.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='input INT').definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_a_case_change_inside_a_quoted_type_supersedes(self):
        """
        Case: The case of a quoted type name changed.
        Expected: SUPERSEDE; quoted identifiers are case-sensitive, so this is a different type and a real overload.
        """
        previous = declare('Doubled', arguments='input "MyType"').definition
        current = declare('Doubled', arguments='input "mytype"').definition

        self.assertIs(current.plan_change_from(previous), Change.SUPERSEDE)

    def test_an_added_out_parameter_with_a_new_return_type_is_a_replace(self):
        """
        Case: An OUT parameter added, which changes what the function returns.
        Expected: REPLACE. OUT parameters are not inputs, so the identity is unchanged and a superseding trailing drop
                  would resolve to the function the leading create just wrote.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', arguments='input TEXT, OUT result TEXT', returns='RECORD').definition

        self.assertIs(current.plan_change_from(previous), Change.REPLACE)


class AltersComputedValuesTestCase(SimpleTestCase):
    def test_a_changed_body_alters_computed_values(self):
        """
        Case: The same function with a new body.
        Expected: Reported as changing what the function computes.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', body='BEGIN RETURN LOWER(input); END;').definition

        self.assertIs(current.alters_computed_values_from(previous), True)

    def test_a_changed_strictness_alters_computed_values(self):
        """
        Case: The same body, but STRICT toggled.
        Expected: Reported as a value change, since STRICT changes what NULL input produces.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', strict=True).definition

        self.assertIs(current.alters_computed_values_from(previous), True)

    def test_a_changed_language_alters_computed_values(self):
        """
        Case: The same body text in a different language.
        Expected: Reported as a value change; the same text means something else to another interpreter.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', language='sql', body='SELECT input;').definition

        self.assertIs(current.alters_computed_values_from(previous), True)

    def test_a_modifier_only_change_does_not(self):
        """
        Case: Only the volatility and parallel safety change.
        Expected: Not a value change, so nothing downstream is rewritten for it.
        """
        previous = declare('Doubled').definition
        current = declare('Doubled', volatility='IMMUTABLE', parallel='SAFE').definition

        self.assertIs(current.alters_computed_values_from(previous), False)
