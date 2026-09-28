"""Resolves the shared ``palmimo-apps`` group id (design doc 2.1).

Both the Portal (``user``) and app processes (``palmimo-app``) belong to
this group; wherever one side must leave a file the other can write or
delete, the caller chowns it to this group. A missing group means a dev
machine with no such group configured -- every caller treats that as
"skip the chown", not a fatal error.
"""

from __future__ import annotations

import grp
import logging


logger = logging.getLogger("palmimo_portal")

APPS_GROUP = "palmimo-apps"


def apps_gid() -> int | None:
    """Return the ``palmimo-apps`` group id, or ``None`` (with a WARNING) if it does not exist on this host."""
    try:
        return grp.getgrnam(APPS_GROUP).gr_gid
    except KeyError:
        logger.warning("apps: group %r does not exist on this host, skipping group ownership", APPS_GROUP)
        return None
