# LMAO Server — Docker image with gRPC API
#
# Build:
#   docker build -t lmao-server .
#
# Run:
#   docker run --rm -it --network host lmao-server
#
# The container uses --network host so that Reticulum can discover
# RNS interfaces (AutoInterface, RNode) and expose gRPC on port 50051.
#
# Note: RNode USB passthrough requires --device /dev/ttyACM0 or similar.

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
