"""
A function declarations module with a broken import, used to pin that such a failure is not read as "this app declares
nothing".
"""

import a_module_that_does_not_exist  # noqa: F401
