from django.db import models
from django.db.models import F

from example.db_functions import AllUppercase
from postgres_objects import GeneratedField


class Cake(models.Model):
    name = models.CharField('name', max_length=128)
    name_uppercased = GeneratedField(
        expression=AllUppercase(F('name')),
        output_field=models.TextField(),
        db_persist=True,
    )

    class Meta:
        app_label = 'example'


class BundtCake(Cake):
    """
    A concrete child of Cake, so its primary key is the parent link rather than a column of its own.
    """

    ring_size = models.IntegerField(default=1)

    class Meta:
        app_label = 'example'


class BundtOrder(models.Model):
    """
    Points at the multi-table child, so selecting its foreign key dereferences through the parent link.
    """

    bundt = models.ForeignKey(BundtCake, on_delete=models.CASCADE)

    class Meta:
        app_label = 'example'
