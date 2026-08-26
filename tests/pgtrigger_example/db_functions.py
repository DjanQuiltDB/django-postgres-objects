from django.db import models

from postgres_objects import Function


class AllUppercase(Function):
    arguments = 'input TEXT'
    returns = 'TEXT'
    volatility = 'IMMUTABLE'
    strict = True
    parallel = 'SAFE'
    output_field = models.TextField()
    body = """
        BEGIN
            RETURN UPPER(input);
        END;
    """
