//! Compile the shared `proto/lma_grpc.proto` into tonic server + prost types.
//! The envelope messages (`LmaoEnvelope` etc.) come from the `lma-wire` crate
//! (prost, `proto/lma_messages.proto`) — `lma_grpc.proto` carries envelopes as
//! opaque `bytes`, so the two are independently codegen'd (DRY, design §6b).

use std::{env, path::PathBuf};

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../proto");
    tonic_build::configure()
        .build_server(true)
        .compile_protos(&[root.join("lma_grpc.proto")], &[root])
        .expect("tonic-build failed to compile proto/lma_grpc.proto");
    println!("cargo:rerun-if-changed=../../../proto/lma_grpc.proto");
}
