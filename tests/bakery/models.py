from django.db import models
from example.models import Cake


class Baker(models.Model):
    name = models.CharField(max_length=128)

    class Meta:
        app_label = 'bakery'


class Recipe(models.Model):
    """
    A model whose foreign keys reach into both apps, so a queryset over it joins across the boundary.
    """

    baker = models.ForeignKey(Baker, on_delete=models.CASCADE)
    cake = models.ForeignKey(Cake, on_delete=models.CASCADE)

    class Meta:
        app_label = 'bakery'
