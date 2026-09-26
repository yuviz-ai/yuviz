"""Importing this package registers every built-in telephony provider with
TelephonyProviderRegistry. Add a new provider by adding a module here and
one import line below — no other file needs to change."""

from . import cloudonix  # noqa: F401
from . import fake  # noqa: F401
from . import vobiz  # noqa: F401
