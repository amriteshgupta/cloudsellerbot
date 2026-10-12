-- SQLite schema version 1. Business transitions are implemented in marketplace/db.py.
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, subscribed INTEGER NOT NULL DEFAULT 0,
 seller INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES users(id),
 name TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL,
 authorization TEXT NOT NULL, delivery TEXT NOT NULL, validity TEXT NOT NULL,
 seller_price INTEGER NOT NULL CHECK(seller_price > 0),
 buyer_price INTEGER NOT NULL CHECK(buyer_price >= seller_price),
 total INTEGER NOT NULL CHECK(total >= 0), negotiable INTEGER NOT NULL DEFAULT 0 CHECK(negotiable IN (0,1)),
 held INTEGER NOT NULL DEFAULT 0 CHECK(held >= 0),
 sold INTEGER NOT NULL DEFAULT 0 CHECK(sold >= 0),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','changes','rejected','paused')),
 reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL,
 CHECK(total >= held + sold));
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 buyer_id INTEGER NOT NULL REFERENCES users(id), seller_id INTEGER NOT NULL REFERENCES users(id),
 product_name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
 seller_price INTEGER NOT NULL, buyer_price INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('reserved','paid','delivered','completed','cancelled','expired')),
 expires INTEGER NOT NULL, created INTEGER NOT NULL, payment_ref TEXT NOT NULL DEFAULT '',
 delivery_note TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
 request_key TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS orders_expiry ON orders(status, expires);
CREATE TABLE IF NOT EXISTS restocks (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 seller_id INTEGER NOT NULL REFERENCES users(id), quantity INTEGER NOT NULL CHECK(quantity > 0),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_events (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
 order_id INTEGER REFERENCES orders(id), actor_id INTEGER NOT NULL,
 kind TEXT NOT NULL, quantity INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
 entity TEXT NOT NULL, entity_id INTEGER NOT NULL, note TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL, buttons TEXT NOT NULL DEFAULT '[]',
 kind TEXT NOT NULL DEFAULT 'notice', product_id INTEGER REFERENCES products(id),
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, created INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state, next_attempt);
PRAGMA user_version=1;
