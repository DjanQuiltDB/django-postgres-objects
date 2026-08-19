"""
A function declarations module reached by a dotted path, to pin that 'db.functions' works as well as 'db_functions'.
"""

from postgres_objects import Function


class Nested(Function):
    arguments = 'input TEXT'
    returns = 'TEXT'
    body = """
        BEGIN
            RETURN input;
        END;
    """
