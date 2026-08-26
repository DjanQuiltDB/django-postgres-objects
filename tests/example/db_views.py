from django.db.models import Count

from example.models import Cake
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


class CakeCounts(MaterializedView):
    """
    A materialized view whose body is a queryset, so its columns are declared exactly once. Importing the app's models
    at the top of this module is fine: queryset() is only called when a definition or a model is needed.
    """

    unique_index = ('name',)

    def queryset():
        return Cake.objects.values('name').annotate(cakes=Count('id'))
