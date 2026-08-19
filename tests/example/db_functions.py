from django.db import models

from postgres_objects import Function


class AllUppercase(Function):
    """
    The declaration the example model's generated column calls. IMMUTABLE and STRICT because Postgres refuses a
    generation expression that is not.
    """

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
