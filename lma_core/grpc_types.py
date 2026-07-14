"""gRPC transport types for LMAO.

These types are only needed by ``lmao_server`` and ``k8s-app``, not by
the human client or Cardputer client.  Importing this module requires
that the protobuf stubs have been generated.

For backward compatibility, these names are also re-exported by
``lma_core`` when available.
"""

import logging
from typing import TYPE_CHECKING

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # These types are only generated when gRPC stubs are compiled.
    # At runtime, they're imported via the try/except below.
    from proto.lma_grpc_pb2_grpc import (  # type: ignore[attr-defined]
        LMAO,
        LMAOServicer,
        LMAOStub,
        add_LMAOServicer_to_server,
    )

    from proto.lma_grpc_pb2 import (  # type: ignore[attr-defined]
        GetIdentityRequest,
        GetIdentityResponse,
        SendRequest,
        SendResponse,
        SubscribeRequest,
        SubscribeResponse,
    )
else:
    # Runtime imports - gracefully handle missing protobuf stubs.
    # These types are only needed for gRPC server functionality;
    # they are NOT re-exported from lma_core.
    try:
        from proto.lma_grpc_pb2 import (  # noqa: F401
            GetIdentityRequest,
            GetIdentityResponse,
            SendRequest,
            SendResponse,
            SubscribeRequest,
            SubscribeResponse,
        )
    except ImportError:
        _logger.warning(
            "gRPC request/response types not found in 'proto.lma_grpc_pb2'. "
            "K8s integration features will be unavailable."
        )

    try:
        from proto.lma_grpc_pb2_grpc import (  # noqa: F401
            LMAO,
            LMAOServicer,
            LMAOStub,
            add_LMAOServicer_to_server,
        )
    except ImportError:
        _logger.warning(
            "gRPC service stubs not found in 'proto.lma_grpc_pb2_grpc'. "
            "K8s integration features will be unavailable."
        )
