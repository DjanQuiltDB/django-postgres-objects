from django.db import models
from django.db.models import F

from example.db_functions import AllUppercase


class Cake(models.Model):
    name = models.CharField('name', max_length=128)
    name_uppercased = models.GeneratedField(
        expression=AllUppercase(F('name')),
        output_field=models.TextField(),
        db_persist=True,
    )

    class Meta:
        app_label = 'example'
