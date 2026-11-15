"""Named model connections on conduit (M1 to M16).

SPARK reaches every model through conduit (``cognisn-conduit``): its providers,
catalogue, residency checks and usage events. This package is SPARK's side of it:

* :mod:`.model`: the connection model (id, name, the kind's conduit settings with
  every credential a ``secret://`` reference, Region, residency policy) and its
  validation through conduit's provider registry;
* :mod:`.refs`: a chosen model as a (connection, model id) pair;
* :mod:`.store`: :class:`~.store.ConnectionStore`, over the database on a server
  and ``config.yaml`` in personal mode;
* :mod:`.catalogue`: conduit's default catalogue plus SPARK's overlay rows;
* :mod:`.runtime`: :class:`~.runtime.ConnectionRuntime`, one conduit provider per
  enabled connection on the process's conduit loop, rebuilt live;
* :mod:`.service`: a model on a connection, called by SPARK's synchronous
  orchestrators through conduit's sync facade;
* :mod:`.usage`: the event sink turning ``model_call_finished`` into usage records;
* :mod:`.upgrade`: 0.2 providers into connections (M12);
* :mod:`.startup`: starting the runtime in a process, and the process's runtime.
"""

from spark.connections.model import Connection, ConnectionInvalid, ConnectionModel
from spark.connections.refs import ModelRef
from spark.connections.runtime import ConnectionRuntime, Snapshot, UserScope
from spark.connections.service import (
    ConnectionService,
    ModelCallFailed,
    ModelRefused,
    ModelUnavailable,
)
from spark.connections.usage import Attribution

__all__ = [
    "Attribution",
    "Connection",
    "ConnectionInvalid",
    "ConnectionModel",
    "ConnectionRuntime",
    "ConnectionService",
    "ModelCallFailed",
    "ModelRef",
    "ModelRefused",
    "ModelUnavailable",
    "Snapshot",
    "UserScope",
]
