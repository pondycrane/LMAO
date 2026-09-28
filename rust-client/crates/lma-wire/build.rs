//! Build-time prost codegen from the shared `proto/lma_messages.proto`
//! (design §6b DRY: the single schema, generated everywhere).
//!
//! Requires `protoc` at build time: either on `PATH` or via the `PROTOC` env
//! var. CI/`bazel run //rust-client:build` passes `PROTOC` pointing at the
//! pinned protoc version (see UPSTREAM.md note in the crate README).

use std::env;
use std::path::PathBuf;

fn main() {
    let proto_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap())
        .join("../../../proto");
    let proto_file = proto_dir.join("lma_messages.proto");

    println!("cargo:rerun-if-changed={}", proto_file.display());

    prost_build::Config::new()
        .compile_protos(&[proto_file], &[proto_dir])
        .expect("prost-build failed to compile proto/lma_messages.proto");
}
