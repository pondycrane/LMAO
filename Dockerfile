# LMAO Server — Docker image with gRPC API + NATS JetStream publishing
#
# Build:
#   docker build -t lmao-server .
#
# Run (production, daemonised):
#   docker run -d --name lmao-server --restart unless-stopped \
#     --network host \
#     -e NATS_SERVER=nats://localhost:4222 \
#     -e LMAO_RNODE_PORT=/dev/ttyACM0 \
#     --device /dev/ttyACM0:/dev/ttyACM0 \
#     lmao-server
#
# Or use: bazel run //tools:install_all -- --include-services
#
# The container uses --network host so that Reticulum can discover
# RNS interfaces (AutoInterface, RNode) and expose gRPC on port 50051.
# RNode USB passthrough requires --device for the serial port.
# NATS_SERVER must point to a reachable NATS server (default: localhost:4222).

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for Reticulum and protobuf
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir \
    grpcio \
    grpcio-tools \
    nats-py \
    protobuf \
    rns \
    lxmf

# Generate protobuf/gRPC stubs
RUN python -m grpc_tools.protoc -I proto --python_out=proto --grpc_python_out=proto proto/lma.proto

# Fix generated import path for package usage
RUN sed -i 's/^import lma_pb2/from proto import lma_pb2/' proto/lma_pb2_grpc.py

# Set PYTHONPATH so proto/ and lma_core/ are importable
ENV PYTHONPATH="/app:${PYTHONPATH}"

EXPOSE 50051

# Run the gRPC-enabled async server
CMD ["python", "-m", "lmao_server.server"]
