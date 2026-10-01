//! SQLite contact book — Rust port of `lma_core/contact_book.py`: the
//! operator-facing "receiver directory". Same table/schema, keys on
//! `delivery_hash`, upsert semantics preserved so an operator name/type
//! survives an auto-learn.

use parking_lot::Mutex;
use rusqlite::{params, Connection};
use std::time::{SystemTime, UNIX_EPOCH};

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS contacts (
    device_name   TEXT PRIMARY KEY,
    device_type   TEXT NOT NULL,
    delivery_hash TEXT NOT NULL UNIQUE,
    pubkey_hex    TEXT,
    identity_hash TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
)";

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct Contact {
    pub device_name: String,
    pub device_type: String,
    pub delivery_hash: String,
    pub pubkey_hex: Option<String>,
    pub identity_hash: Option<String>,
    pub first_seen: f64,
    pub last_seen: f64,
}

fn row_to_contact(row: &rusqlite::Row) -> rusqlite::Result<Contact> {
    Ok(Contact {
        device_name: row.get("device_name")?,
        device_type: row.get("device_type")?,
        delivery_hash: row.get("delivery_hash")?,
        pubkey_hex: row.get("pubkey_hex")?,
        identity_hash: row.get("identity_hash")?,
        first_seen: row.get("first_seen")?,
        last_seen: row.get("last_seen")?,
    })
}

pub fn now_f() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Thread-safe SQLite contact book (single table + global lock — same
/// simplicity contract as the Python original).
pub struct ContactBook {
    conn: Mutex<Connection>,
}

impl ContactBook {
    pub fn open(db_path: &str) -> rusqlite::Result<Self> {
        let conn = Connection::open(db_path)?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    #[cfg(test)]
    pub fn open_in_memory() -> rusqlite::Result<Self> {
        let conn = Connection::open_in_memory()?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// Upsert a contact keyed by `delivery_hash`. `device_name` defaults to
    /// `<type>-<last4>` so a contact is reachable before being named; an
    /// operator-set name is honored on later register() calls.
    pub fn register(
        &self,
        delivery_hash: &str,
        device_type: &str,
        device_name: Option<&str>,
        pubkey_hex: Option<&str>,
        identity_hash: Option<&str>,
    ) -> rusqlite::Result<Contact> {
        let dh = delivery_hash.to_lowercase();
        // Python: default name is `<type>-<last4 of the passed hash>` (raw arg).
        let name = device_name
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{device_type}-{}", &delivery_hash[delivery_hash.len().saturating_sub(4)..]));
        let now = now_f();
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO contacts(
                device_name, device_type, delivery_hash,
                pubkey_hex, identity_hash, first_seen, last_seen
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
             ON CONFLICT(delivery_hash) DO UPDATE SET
                device_name = CASE WHEN excluded.device_name LIKE excluded.device_type || '-%'
                                   THEN excluded.device_name
                                   ELSE contacts.device_name END,
                device_type = excluded.device_type,
                pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                identity_hash = COALESCE(excluded.identity_hash, contacts.identity_hash),
                last_seen = excluded.last_seen",
            params![name, device_type, dh, pubkey_hex, identity_hash, now, now],
        )?;
        let mut stmt = conn.prepare("SELECT * FROM contacts WHERE delivery_hash = ?1")?;
        let mut rows = stmt.query(params![dh])?;
        let row = rows.next()?.ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        row_to_contact(row)
    }

    /// Refresh `last_seen`; false if the device is not known.
    pub fn touch(&self, delivery_hash: &str) -> rusqlite::Result<bool> {
        let conn = self.conn.lock();
        let n = conn.execute(
            "UPDATE contacts SET last_seen = ?1 WHERE delivery_hash = ?2",
            params![now_f(), delivery_hash.to_lowercase()],
        )?;
        Ok(n > 0)
    }

    pub fn is_known(&self, delivery_hash: &str) -> rusqlite::Result<bool> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare("SELECT 1 FROM contacts WHERE delivery_hash = ?1")?;
        let mut rows = stmt.query(params![delivery_hash.to_lowercase()])?;
        Ok(rows.next()?.is_some())
    }

    pub fn find(
        &self,
        delivery_hash: Option<&str>,
        device_name: Option<&str>,
        device_type: Option<&str>,
    ) -> rusqlite::Result<Vec<Contact>> {
        let mut clauses = Vec::new();
        let mut args: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        if let Some(dh) = delivery_hash {
            clauses.push("delivery_hash = ?");
            args.push(Box::new(dh.to_lowercase()));
        }
        if let Some(n) = device_name {
            clauses.push("device_name = ?");
            args.push(Box::new(n.to_string()));
        }
        if let Some(t) = device_type {
            clauses.push("device_type = ?");
            args.push(Box::new(t.to_string()));
        }
        let where_clause = if clauses.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", clauses.join(" AND "))
        };
        let sql = format!("SELECT * FROM contacts{where_clause} ORDER BY device_name");
        self.query_rows(&sql, args)
    }

    pub fn all(&self) -> rusqlite::Result<Vec<Contact>> {
        self.query_rows("SELECT * FROM contacts ORDER BY device_name", Vec::new())
    }

    fn query_rows(
        &self,
        sql: &str,
        args: Vec<Box<dyn rusqlite::ToSql>>,
    ) -> rusqlite::Result<Vec<Contact>> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(sql)?;
        let refs: Vec<&dyn rusqlite::ToSql> = args.iter().map(|b| b.as_ref()).collect();
        let rows = stmt.query_map(refs.as_slice(), row_to_contact)?;
        rows.collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_then_find() {
        let book = ContactBook::open_in_memory().unwrap();
        let c = book
            .register("abc123", "sprout", None, Some("deadbeef"), None)
            .unwrap();
        assert_eq!(c.device_name, "sprout-c123"); // <type>-<last4 of raw arg>
        assert_eq!(c.device_type, "sprout");
        assert!(book.is_known("ABC123").unwrap()); // case-insensitive key
        assert!(!book.is_known("zzz").unwrap());
        assert_eq!(book.all().unwrap().len(), 1);
        assert_eq!(book.find(Some("ABC123"), None, None).unwrap().len(), 1);
        assert_eq!(book.find(None, Some("sprout-c123"), None).unwrap().len(), 1);
        assert_eq!(book.find(None, None, Some("nope")).unwrap().len(), 0);
    }

    #[test]
    fn operator_name_persists_through_learn_touch() {
        let book = ContactBook::open_in_memory().unwrap();
        // Operator names the device on first registration (INSERT row).
        let c = book
            .register("ab12cd34", "sprout", Some("Greenhouse"), None, None)
            .unwrap();
        assert_eq!(c.device_name, "Greenhouse");
        // The learn path for a known device is touch() — name untouched.
        assert!(book.is_known("ab12cd34").unwrap());
        assert!(book.touch("ab12cd34").unwrap());
        assert_eq!(
            book.find(Some("ab12cd34"), None, None).unwrap()[0].device_name,
            "Greenhouse"
        );
        // (A direct re-register with the default `device-xxxx` name reverts to
        // the type-prefixed name — faithful to the Python ON CONFLICT CASE.)
        let c2 = book.register("ab12cd34", "device", None, None, None).unwrap();
        assert_eq!(c2.device_name, "device-cd34");
    }

    #[test]
    fn pubkey_coalesce() {
        let book = ContactBook::open_in_memory().unwrap();
        book.register("d1", "device", None, Some("aabb"), None).unwrap();
        let c = book.register("d1", "device", None, None, None).unwrap();
        assert_eq!(c.pubkey_hex.as_deref(), Some("aabb"));
    }

    #[test]
    fn missing_touch_returns_false() {
        let book = ContactBook::open_in_memory().unwrap();
        assert!(!book.touch("missing").unwrap());
        assert!(book.all().unwrap().is_empty());
    }
}
