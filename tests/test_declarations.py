from django.core.exceptions import ImproperlyConfigured
from django.db.models import CharField, F, Func
from django.test import SimpleTestCase
from example.db_functions import AllUppercase
from example.more_functions import InheritedBody
from outside_app_fixture import Homeless, ReusableBody

from postgres_objects import Function
from postgres_objects.base import MAX_IDENTIFIER_LENGTH, quote_name


class NameResolutionTestCase(SimpleTestCase):
    def test_the_name_comes_from_the_class_name(self):
        """
        Case: A declaration that does not set a name.
        Expected: The name is the lowercased class name.
        """
        self.assertEqual(AllUppercase.name, 'alluppercase')

    def test_an_explicit_name_wins(self):
        """
        Case: A declaration that sets a name of its own.
        Expected: The class name is not consulted.
        """

        class Whatever(Function):
            name = 'pinned_name'

        self.assertEqual(Whatever.name, 'pinned_name')

    def test_the_db_name_is_namespaced_by_app(self):
        """
        Case: A declaration in the example app.
        Expected: The identifier it is created under carries the app label.
        """
        self.assertEqual(AllUppercase.resolved_db_name, 'example_alluppercase')

    def test_an_explicit_db_name_wins(self):
        """
        Case: A declaration that pins its db_name.
        Expected: It is used verbatim, without the app label.
        """

        class Whatever(Function):
            app_label = 'example'
            db_name = 'alluppercase'

        self.assertEqual(Whatever.resolved_db_name, 'alluppercase')

    def test_a_long_db_name_is_truncated(self):
        """
        Case: An app label and name that together exceed the Postgres identifier limit.
        Expected: The generated identifier is truncated to the limit.
        """

        class Whatever(Function):
            app_label = 'example'
            name = 'x' * 100

        self.assertEqual(len(Whatever.resolved_db_name), MAX_IDENTIFIER_LENGTH)

    def test_a_subclass_gets_its_own_name(self):
        """
        Case: A concrete declaration that only subclasses an abstract one.
        Expected: The name comes from the subclass, the body from the base.
        """
        self.assertEqual(InheritedBody.name, 'inheritedbody')
        self.assertEqual(InheritedBody.returns, 'TEXT')


class AppResolutionTestCase(SimpleTestCase):
    def test_the_app_comes_from_the_declaring_module(self):
        """
        Case: A declaration written in an installed app.
        Expected: It is attributed to that app, without any stack inspection.
        """
        self.assertEqual(AllUppercase.resolved_app_label, 'example')

    def test_a_subclass_is_attributed_to_its_own_module(self):
        """
        Case: A concrete declaration whose abstract base lives outside every installed app.
        Expected: The subclass's own module decides the app, so the base being homeless does not matter.
        """
        self.assertEqual(InheritedBody.__module__, 'example.more_functions')
        self.assertEqual(InheritedBody.resolved_app_label, 'example')

    def test_an_explicit_app_label_wins(self):
        """
        Case: A declaration that sets app_label.
        Expected: The declaring module is not consulted.
        """

        class Whatever(Function):
            app_label = 'example'

        self.assertEqual(Whatever.resolved_app_label, 'example')

    def test_a_declaration_outside_every_app_is_refused(self):
        """
        Case: A declaration in a module that belongs to no installed app.
        Expected: Resolving the app label raises, naming the way out.
        """
        with self.assertRaises(ImproperlyConfigured) as caught:
            Homeless.resolved_app_label

        self.assertIn('Homeless', str(caught.exception))
        self.assertIn('app_label', str(caught.exception))


class AbstractTestCase(SimpleTestCase):
    def test_abstract_is_not_inherited(self):
        """
        Case: A declaration subclassing an abstract one without saying anything about abstract.
        Expected: The subclass is concrete.
        """
        self.assertTrue(ReusableBody.abstract)
        self.assertFalse(InheritedBody.abstract)

    def test_an_abstract_declaration_has_no_name(self):
        """
        Case: An abstract declaration.
        Expected: No name is derived from its class name, so it cannot be mistaken for a real object.
        """
        self.assertIsNone(ReusableBody.name)
        self.assertIsNone(Function.name)

    def test_an_abstract_declaration_has_no_definition(self):
        """
        Case: Ask an abstract declaration for its definition.
        Expected: TypeError, rather than a definition naming nothing.
        """
        with self.assertRaises(TypeError):
            ReusableBody.definition

    def test_an_abstract_declaration_is_not_callable_as_an_expression(self):
        """
        Case: Use an abstract declaration as a queryset expression.
        Expected: TypeError, since it names no function.
        """
        with self.assertRaises(TypeError):
            ReusableBody(F('name'))


class InstantiationTestCase(SimpleTestCase):
    def test_a_declaration_builds_an_expression_rather_than_an_instance(self):
        """
        Case: Call a concrete declaration.
        Expected: A Func naming the function unqualified, not an instance of the declaration.
        """
        expression = AllUppercase(F('name'))

        self.assertIsInstance(expression, Func)
        # Func keeps the name in extra, which is where its template reads it from.
        self.assertEqual(expression.extra['function'], 'example_alluppercase')

    def test_the_output_field_defaults_to_the_declared_one(self):
        """
        Case: Call a declaration that declares an output_field.
        Expected: The expression knows its result type without it being repeated.
        """
        self.assertEqual(AllUppercase(F('name')).output_field.get_internal_type(), 'TextField')

    def test_the_output_field_can_be_overridden_per_call(self):
        """
        Case: Pass an output_field at the call site.
        Expected: It wins over the declared one.
        """
        expression = AllUppercase(F('name'), output_field=CharField(max_length=10))

        self.assertEqual(expression.output_field.get_internal_type(), 'CharField')

    def test_a_non_function_declaration_refuses_instantiation(self):
        """
        Case: Instantiate a declaration of a kind that has no expression meaning.
        Expected: TypeError pointing at the definition, so the mistake is not silently a value.
        """
        from postgres_objects.base import DeclarativeObject

        class Whatever(DeclarativeObject):
            app_label = 'example'

        with self.assertRaises(TypeError) as caught:
            Whatever()

        self.assertIn('definition', str(caught.exception))


class QuoteNameTestCase(SimpleTestCase):
    def test_a_bare_name_is_wrapped(self):
        """
        Case: The generated, lowercase kind of name.
        Expected: Wrapped in double quotes, which Postgres reads as the same identifier the bare spelling folds to.
        """
        self.assertEqual(quote_name('example_totals'), '"example_totals"')

    def test_case_is_preserved(self):
        """
        Case: A mixed-case name.
        Expected: Wrapped verbatim, making the identifier case-sensitive like a mixed-case db_table.
        """
        self.assertEqual(quote_name('MyTotals'), '"MyTotals"')

    def test_an_already_quoted_name_passes_through(self):
        """
        Case: A name already wrapped in quotes.
        Expected: Unchanged. Quoting once is enough, mirroring Django's own quote_name.
        """
        self.assertEqual(quote_name('"pinned"'), '"pinned"')


class FunctionValidationTestCase(SimpleTestCase):
    def declare(self, class_name, **attrs):
        namespace = {
            'app_label': 'example',
            'arguments': 'input TEXT',
            'returns': 'TEXT',
            'body': 'BEGIN RETURN input; END;',
        }
        namespace.update(attrs)

        return type(Function)(class_name, (Function,), namespace)

    def test_a_missing_returns_refuses_to_build(self):
        """
        Case: A declaration that forgot returns.
        Expected: TypeError naming the class.
        """
        declaration = self.declare('Forgetful', returns=None)

        with self.assertRaisesMessage(TypeError, 'Forgetful'):
            declaration.definition

    def test_a_missing_body_refuses_to_build(self):
        """
        Case: A declaration that forgot its body.
        Expected: TypeError naming the class.
        """
        declaration = self.declare('Forgetful', body=None)

        with self.assertRaisesMessage(TypeError, 'Forgetful'):
            declaration.definition

    def test_a_complete_declaration_builds(self):
        """
        Case: A declaration carrying everything.
        Expected: A definition, successfully.
        """
        self.assertEqual(self.declare('Complete').definition.returns, 'TEXT')

    def test_an_abstract_declaration_keeps_its_abstract_message(self):
        """
        Case: The definition of an abstract declaration, which naturally has no returns or body either.
        Expected: The abstract message.
        """
        with self.assertRaisesMessage(TypeError, 'Function is abstract, so it has no definition.'):
            Function.definition
