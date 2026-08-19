import pgtrigger
from django.db import models
from django.db.models import F

from pgtrigger_example.db_functions import AllUppercase


class Note(models.Model):
    """
    A model both libraries have something to say about: a generated column calling a declared function, and a trigger.
    """

    text = models.CharField(max_length=128)
    text_uppercased = models.GeneratedField(
        expression=AllUppercase(F('text')),
        output_field=models.TextField(),
        db_persist=True,
    )

    class Meta:
        app_label = 'pgtrigger_example'
        triggers = [pgtrigger.Protect(name='protect_deletes', operation=pgtrigger.Delete)]
