from django.db.models import F
from example.db_views import CakeCounts

from bakery.models import Recipe
from postgres_objects import View


class RecipeCredits(View):
    """
    A view joining two models, one of them another app's. The queryset is what says so, which is what the autodetector
    turns into the dependency on the other app's migrations.
    """

    def queryset():
        return Recipe.objects.values('id', baker_name=F('baker__name'), cake_name=F('cake__name'))


class PopularCakes(View):
    """
    A view built on another app's view, through the unmanaged model that view generates.
    """

    primary_key = 'name'

    def queryset():
        return CakeCounts.objects.filter(cakes__gt=1).values('name', 'cakes')
