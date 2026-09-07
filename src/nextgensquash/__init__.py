"""nextgensquash: propose, emit, install, and retire a from-scratch squash of pre-cutoff Django migrations.

Importing this package has no side effects. The CLI sets up Django; library
callers must call ``django.setup()`` themselves before using the submodules.
"""

__version__ = "0.1.0"
