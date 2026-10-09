=============
System checks
=============

``manage.py check`` runs these, and so do ``runserver`` and ``migrate``. Each one reports a mistake that would otherwise
surface late (at migrate time, or at the first query) or never.

============================ ==========================================================================================
``postgres_objects.W001``    The ``POSTGRES_OBJECTS`` setting contains an unknown key. A typo there fails nothing on its
                             own (the key is simply never read) so the module it meant to name would silently not be
                             managed at all. The hint names the closest known key. See :doc:`/reference/settings`.
``postgres_objects.E001``    A queryset-declared view could not be compiled. See :doc:`/guides/views`.
``postgres_objects.E002``    The model for a queryset-declared view could not be built, for example because no
                             primary key can be worked out. See :doc:`/guides/views`.
``postgres_objects.E003``    ``postgres_objects.GeneratedField`` is used, but ``FUNCTIONS_MODULE_PATH`` names no
                             module, so no declaration is managed anywhere in the project. See
                             :doc:`/guides/generated_columns`.
``postgres_objects.E004``    ``recalculate_on`` names something no declaration is called, such as a typo or a
                             declaration that has since been deleted. See :doc:`/guides/generated_columns`.
``postgres_objects.E005``    The generated column is ``VIRTUAL``. It can never reference a declaration, so it can never
                             be recalculated. See :doc:`/guides/generated_columns`.
``postgres_objects.E006``    The generated column resolves no declaration from either its expression or
                             ``recalculate_on``. See :doc:`/guides/generated_columns`.
``postgres_objects.E007``    The view declaration depends on something that names no relation: a ``depends_on`` entry
                             that is not a ``View`` declaration, a model or a table name, or one naming an abstract
                             declaration. See :doc:`/guides/views`.
``postgres_objects.E008``    The function declaration could not be built. Its ``returns`` or ``body`` is missing, or
                             building its definition raised. See :doc:`/guides/functions`.
============================ ==========================================================================================
