"""
Declarations living outside every installed app.

The abstract one here is subclassed from inside an app by example.more_functions, which is what pins that the owning app
is decided by the subclass's own module rather than the base's.
"""

from postgres_objects import Function


class ReusableBody(Function):
    """
    An abstract declaration meant to be subclassed. It must never be collected or asked for a definition itself.
    """

    abstract = True

    arguments = 'input TEXT'
    returns = 'TEXT'
    body = """
        BEGIN
            RETURN input;
        END;
    """


class Homeless(ReusableBody):
    """
    Concrete, but in a module belonging to no installed app, so resolving its app label has to fail loudly.
    """

    pass
