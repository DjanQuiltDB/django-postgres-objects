"""
A second function declarations module for the example app, holding a declaration that is concrete purely by subclassing
a base written outside every app.
"""

from outside_app_fixture import ReusableBody


class InheritedBody(ReusableBody):
    """
    Declares nothing of its own: every attribute comes from the abstract base, while the name and the owning app come
    from this class and this module.
    """

    pass
