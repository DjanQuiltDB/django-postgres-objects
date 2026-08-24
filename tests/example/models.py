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
