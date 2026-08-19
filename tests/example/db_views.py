from postgres_objects import MaterializedView, View


class UppercasedCakes(View):
    """
    A plain view over the example model's table.
    """

    sql = 'SELECT id, name_uppercased FROM example_cake'


class StackedCakes(View):
    """
    A view selecting from another view, declared below the one it reads so that the creation order works out.
    """

    sql = 'SELECT id FROM example_uppercasedcakes'


class CakeTotals(MaterializedView):
    """
    A materialized view with the unique index a concurrent refresh needs.
    """

    sql = 'SELECT name, count(*) AS cakes FROM example_cake GROUP BY name'
    unique_index = ('name',)
    indexes = [('cakes',)]
