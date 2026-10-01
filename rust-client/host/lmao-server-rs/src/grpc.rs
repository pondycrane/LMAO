//! tonic gRPC `LMAO` service — Rust port of `lmao_server.Server.LMAOGrpcService`
//! (Send / Subscribe stream / GetIdentity).

use std::pin::Pin;
use std::sync::Arc;

use lma_wire::lmao_envelope::Payload;
use lma_wire::LmaoEnvelope;
use prost::Message;
use tokio::sync::mpsc;
use tokio_stream::wrappers::UnboundedReceiverStream;
use tonic::{Request, Response, Status};

use crate::delivery::{DeliveryEvent, SharedState};
use crate::lma::lmao_server::Lmao;
use crate::lma::{
    GetIdentityRequest, GetIdentityResponse, SendRequest, SendResponse, SubscribeRequest,
    SubscribeResponse,
};

pub struct LmaoGrpcService {
    pub state: SharedState,
}

#[tonic::async_trait]
impl Lmao for LmaoGrpcService {
    type SubscribeStream = Pin<
        Box<
            dyn tokio_stream::Stream<Item = Result<SubscribeResponse, Status>>
                + Send
                + 'static,
        >,
    >;

    async fn send(&self, request: Request<SendRequest>) -> Result<Response<SendResponse>, Status> {
        let envelope_bytes = request.into_inner().envelope;
        let envelope = LmaoEnvelope::decode(envelope_bytes.as_slice())
            .map_err(|e| Status::invalid_argument(format!("Bad envelope: {e}")))?;

        // The destination comes from the envelope payload (CommandRequest.target).
        let dest_hash = envelope
            .payload
            .as_ref()
            .and_then(|p| match p {
                Payload::Command(c) => Some(c.target.clone()),
                _ => None,
            })
            .filter(|t| !t.is_empty())
            .unwrap_or_default();

        if dest_hash.is_empty() {
            return Ok(Response::new(SendResponse {
                destination_hash: String::new(),
                status: "error: invalid or unreachable destination".into(),
            }));
        }

        match self
            .state
            .mesh
            .send(&dest_hash, &envelope_bytes, "p:Envelope")
            .await
        {
            Ok(()) => Ok(Response::new(SendResponse {
                destination_hash: dest_hash,
                status: "queued".into(),
            })),
            Err(e) => Err(Status::internal(format!("Send failed: {e}"))),
        }
    }

    async fn subscribe(
        &self,
        request: Request<SubscribeRequest>,
    ) -> Result<Response<Self::SubscribeStream>, Status> {
        let title_filter = request.into_inner().title_filter;
        let state = Arc::clone(&self.state);

        let (tx, rx) = mpsc::unbounded_channel::<Result<SubscribeResponse, Status>>();
        let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<DeliveryEvent>();
        state.register_subscriber(ev_tx.clone());

        // Forward delivery events to this subscriber, applying the title filter;
        // deregister once the client (or fanout) closes the channel.
        tokio::spawn(async move {
            while let Some(ev) = ev_rx.recv().await {
                if !title_filter.is_empty() && !ev.title.contains(&title_filter) {
                    continue;
                }
                if tx
                    .send(Ok(SubscribeResponse {
                        envelope: ev.envelope.into(),
                        source_hash: ev.source_hash,
                    }))
                    .is_err()
                {
                    break;
                }
            }
            state.unregister_subscriber(&ev_tx);
        });

        Ok(Response::new(Box::pin(UnboundedReceiverStream::new(rx))))
    }

    async fn get_identity(
        &self,
        _request: Request<GetIdentityRequest>,
    ) -> Result<Response<GetIdentityResponse>, Status> {
        Ok(Response::new(GetIdentityResponse {
            identity_hex: self.state.server_identity_hex.clone(),
            node_name: self.state.node_name.clone(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::delivery::{AppState, LogMesh};
    use crate::store::ContactBook;

    fn test_grpc() -> LmaoGrpcService {
        LmaoGrpcService {
            state: Arc::new(AppState::new(
                ContactBook::open_in_memory().unwrap(),
                Default::default(),
                "deadbeefdeadbeefdeadbeefdeadbeef".into(),
                "lmao-server".into(),
                None,
                Box::new(LogMesh),
            )),
        }
    }

    #[tokio::test]
    async fn get_identity_returns_server_hash_and_name() {
        let svc = test_grpc();
        let resp = svc
            .get_identity(Request::new(GetIdentityRequest {}))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.identity_hex, "deadbeefdeadbeefdeadbeefdeadbeef");
        assert_eq!(resp.node_name, "lmao-server");
    }

    #[tokio::test]
    async fn send_rejects_bad_envelope() {
        let svc = test_grpc();
        let r = svc
            .send(Request::new(SendRequest {
                envelope: b"not a protobuf".to_vec(),
            }))
            .await;
        assert!(r.is_err());
        assert_eq!(r.unwrap_err().code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn send_without_command_target_reports_error() {
        let svc = test_grpc();
        // An LMAOEnvelope with no command payload -> no destination.
        let env = LmaoEnvelope { payload: None };
        let resp = svc
            .send(Request::new(SendRequest {
                envelope: env.encode_to_vec(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.status.starts_with("error"));
    }

    #[tokio::test]
    async fn subscribe_streams_fanout_events() {
        use futures::StreamExt;
        let svc = test_grpc();
        let resp = svc
            .subscribe(Request::new(SubscribeRequest {
                title_filter: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();
        let mut stream = resp;

        // Push a delivery event from a different task and read it on the stream.
        let state = Arc::clone(&svc.state);
        tokio::spawn(async move {
            state.fanout(DeliveryEvent {
                envelope: b"hello".to_vec(),
                source_hash: "aa".into(),
                title: "p:Envelope".into(),
            });
        });
        let first = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            stream.next(),
        )
        .await
        .expect("stream should yield within timeout")
        .expect("stream open");
        let msg = first.unwrap();
        assert_eq!(msg.envelope.as_slice(), b"hello");
        assert_eq!(msg.source_hash, "aa");
    }
}
