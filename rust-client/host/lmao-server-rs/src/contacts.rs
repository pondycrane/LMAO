//! Contacts HTTP API (receiver directory) — Rust port of
//! `lma_core/contacts_api.py` (aiohttp → axum, same routes + status codes):
//!
//!   GET  /contacts          list every known device
//!   GET  /contacts/find?hash=..&name=..&type=..   filter (AND)
//!   POST /contacts          upsert {delivery_hash, type, name?, pubkey_hex?, identity_hash?}

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;

use crate::delivery::AppState;

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/contacts", get(list_contacts).post(register_contact))
        .route("/contacts/find", get(find_contact))
        .with_state(state)
}

async fn list_contacts(State(state): State<Arc<AppState>>) -> Json<Value> {
    match state.contact_book.all() {
        Ok(contacts) => Json(json!({ "contacts": contacts })),
        Err(e) => Json(json!({ "error": e.to_string() })),
    }
}

#[derive(Deserialize)]
struct FindQuery {
    hash: Option<String>,
    name: Option<String>,
    #[serde(rename = "type")]
    type_: Option<String>,
}

async fn find_contact(
    State(state): State<Arc<AppState>>,
    Query(q): Query<FindQuery>,
) -> Json<Value> {
    match state
        .contact_book
        .find(q.hash.as_deref(), q.name.as_deref(), q.type_.as_deref())
    {
        Ok(contacts) => Json(json!({ "contacts": contacts })),
        Err(e) => Json(json!({ "error": e.to_string() })),
    }
}

#[derive(Deserialize)]
struct RegisterBody {
    #[serde(rename = "delivery_hash")]
    delivery_hash: String,
    #[serde(rename = "type")]
    type_: Option<String>,
    #[serde(rename = "device_type")]
    device_type: Option<String>,
    name: Option<String>,
    pubkey_hex: Option<String>,
    identity_hash: Option<String>,
}

async fn register_contact(
    State(state): State<Arc<AppState>>,
    Json(body): Json<RegisterBody>,
) -> Response {
    let device_type = body.type_.clone().or_else(|| body.device_type.clone());
    if body.delivery_hash.is_empty() || device_type.is_none() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({ "error": "Both 'delivery_hash' and 'type' are required." })),
        )
            .into_response();
    }
    match state.contact_book.register(
        &body.delivery_hash,
        &device_type.unwrap(),
        body.name.as_deref(),
        body.pubkey_hex.as_deref(),
        body.identity_hash.as_deref(),
    ) {
        Ok(contact) => (StatusCode::CREATED, Json(json!({ "contact": contact }))).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": e.to_string() })),
        )
            .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::delivery::LogMesh;
    use crate::store::ContactBook;
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::util::ServiceExt;

    fn test_state() -> Arc<AppState> {
        Arc::new(AppState::new(
            ContactBook::open_in_memory().unwrap(),
            Default::default(),
            "id".into(),
            "lmao-server".into(),
            None,
            Box::new(LogMesh),
        ))
    }

    async fn body_json(resp: axum::response::Response) -> Value {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn list_contacts_empty() {
        let app = router(test_state());
        let resp = app
            .oneshot(Request::builder().uri("/contacts").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["contacts"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn register_then_list_and_find() {
        let app = router(test_state());
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/contacts")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"delivery_hash":"ab12cd34","type":"sprout","name":"Greenhouse"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let v = body_json(resp).await;
        assert_eq!(v["contact"]["device_name"], "Greenhouse");
        assert_eq!(v["contact"]["device_type"], "sprout");

        let list = app
            .clone()
            .oneshot(Request::builder().uri("/contacts").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(body_json(list).await["contacts"].as_array().unwrap().len(), 1);

        let find = app
            .oneshot(
                Request::builder()
                    .uri("/contacts/find?type=sprout")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let fv = body_json(find).await;
        assert_eq!(fv["contacts"].as_array().unwrap().len(), 1);
        assert_eq!(fv["contacts"][0]["delivery_hash"], "ab12cd34");
    }

    #[tokio::test]
    async fn register_requires_both_fields() {
        let app = router(test_state());
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/contacts")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"delivery_hash":"ab12cd34"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let v = body_json(resp).await;
        assert!(v["error"].as_str().unwrap().contains("required"));
    }
}
