import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .policy import buyer_cents, money, validate_text

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT, subscribed INTEGER NOT NULL DEFAULT 0,
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
'''


class Store:
    def __init__(self, path, admins=(), currency='USD'):
        self.path, self.admins, self.currency = str(path), frozenset(admins), currency
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(path).exists():
            Path(path).touch(mode=0o600)
        c = self.connect()
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)
            columns = {row['name'] for row in c.execute('PRAGMA table_info(products)')}
            if 'negotiable' not in columns:
                c.execute('ALTER TABLE products ADD COLUMN negotiable INTEGER NOT NULL DEFAULT 0 CHECK(negotiable IN (0,1))')
        finally:
            c.close()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        c = self.connect()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def admin(self, actor):
        if actor not in self.admins:
            raise ValueError('Admin permission required.')

    def active(self, c, actor, seller=False):
        u = c.execute('SELECT * FROM users WHERE id=?', (actor,)).fetchone()
        if not u or u['banned'] or (seller and not u['seller']):
            raise ValueError('Permission denied. Use /start and /sell; banned users cannot trade.')

    def register(self, uid, username):
        with self.tx() as c:
            c.execute('INSERT INTO users(id,username) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username', (uid, username))

    def set_user(self, uid, field, value):
        if field != 'seller':
            raise ValueError('Invalid field')
        with self.tx() as c:
            self.active(c, uid)
            c.execute(f'UPDATE users SET {field}=? WHERE id=?', (int(value), uid))

    def enqueue(self, c, chat, body, buttons=(), kind='notice', product_id=None):
        c.execute('INSERT INTO outbox(chat_id,body,buttons,kind,product_id,created) VALUES (?,?,?,?,?,?)',
                  (chat, body, json.dumps(buttons), kind, product_id, int(time.time())))

    def audit(self, c, actor, action, entity, eid, note=''):
        c.execute('INSERT INTO audit(actor_id,action,entity,entity_id,note,created) VALUES (?,?,?,?,?,?)',
                  (actor, action, entity, eid, note, int(time.time())))

    def event(self, c, pid, actor, kind, quantity, oid=None):
        c.execute('INSERT INTO inventory_events(product_id,order_id,actor_id,kind,quantity,created) VALUES (?,?,?,?,?,?)',
                  (pid, oid, actor, kind, quantity, int(time.time())))

    def product(self, pid):
        return self.one('SELECT * FROM products WHERE id=?', (pid,))

    def public_text(self, p):
        available = p['total'] - p['held'] - p['sold']
        negotiable = 'Yes' if p['negotiable'] else 'No'
        return (f"#{p['id']} — {p['name']}\nAvailable stock: {available}\n"
            f"Price per unit: {money(p['buyer_price'], self.currency)}\n"
            f"Negotiable: {negotiable}\nDelivery: {p['delivery']}")

    def review_text(self, p):
        return (f"LISTING #{p['id']} — {p['status']}\nSeller ID: {p['seller_id']}\n" + self.public_text(p) +
                f"\nSeller unit price: {money(p['seller_price'], self.currency)}\n"
                f"Resale rights evidence: {p['authorization']}\nReview rights and policy before approving.")

    def submit(self, seller, data, edit_id=None):
        fields = ('name','category','description','authorization','delivery','validity')
        limits = {'name': (3,100), 'category': (2,60), 'description': (10,1000),
                  'authorization': (10,800), 'delivery': (1,150), 'validity': (1,150)}
        for f in fields:
            lower, upper = limits[f]
            if not isinstance(data[f], str) or not lower <= len(data[f]) <= upper:
                raise ValueError(f'Invalid {f} length.')
            validate_text(data[f])
        price, qty = data['seller_price'], data['total']
        negotiable = data.get('negotiable', False)
        if not isinstance(negotiable, bool):
            raise ValueError('Negotiable must be true or false.')
        if not isinstance(price, int) or not 1 <= price <= 100_000_000 or not isinstance(qty, int) or not 1 <= qty <= 10000:
            raise ValueError('Invalid price or quantity.')
        with self.tx() as c:
            self.active(c, seller, seller=True)
            values = tuple(data[f] for f in fields) + (price, buyer_cents(price), qty, int(negotiable))
            if edit_id:
                p = c.execute('SELECT * FROM products WHERE id=?', (edit_id,)).fetchone()
                if not p or p['seller_id'] != seller or p['status'] not in ('pending','changes','rejected','approved','paused'):
                    raise ValueError('Only your submitted listings can be edited.')
                if qty < p['held'] + p['sold']:
                    raise ValueError('Quantity cannot be lower than held plus sold stock.')
                c.execute('UPDATE products SET name=?,category=?,description=?,authorization=?,delivery=?,validity=?,seller_price=?,buyer_price=?,total=?,negotiable=?,status="pending",reason="",revision=revision+1 WHERE id=?', values + (edit_id,))
                pid = edit_id
                self.event(c, pid, seller, 'resubmission', qty - p['total'])
            else:
                pid = c.execute('INSERT INTO products(seller_id,name,category,description,authorization,delivery,validity,seller_price,buyer_price,total,negotiable,status,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,"pending",?)',
                                (seller,) + values + (int(time.time()),)).lastrowid
                self.event(c, pid, seller, 'submission', qty)
            p = dict(c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone())
            for admin in self.admins:
                self.enqueue(c, admin, self.review_text(p), [('Review listing', f'review:{pid}')])
            self.audit(c, seller, 'submit', 'product', pid)
            return pid

    def contact_admin(self, buyer, pid):
        with self.tx() as c:
            self.active(c, buyer)
            p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            if not p or p['status'] != 'approved' or p['total'] - p['held'] - p['sold'] <= 0:
                raise ValueError('Product is no longer available.')
            body = (f"Buyer {buyer} is interested in product #{pid}: {p['name']}\n"
                    f"Available stock: {p['total'] - p['held'] - p['sold']}\n"
                    f"Ask the buyer for quantity and arrange the deal manually.")
            for admin in self.admins:
                self.enqueue(c, admin, body)
            return dict(p)

    def moderate(self, actor, pid, action, reason='', expected_revision=None):
        self.admin(actor)
        targets = {'approve': ('pending','approved'), 'reject': ('pending','rejected'),
                   'changes': ('pending','changes'), 'pause': ('approved','paused'), 'resume': ('paused','approved')}
        if action not in targets:
            raise ValueError('Invalid moderation action.')
        if action in ('reject','changes') and not reason.strip():
            raise ValueError('A reason is required.')
        with self.tx() as c:
            p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            before, after = targets[action]
            if not p or p['status'] != before or (expected_revision is not None and p['revision'] != expected_revision):
                raise ValueError('Listing status changed. Refresh /admin.')
            if after == 'approved':
                self.active(c, p['seller_id'], seller=True)
                for field in ('name','category','description','authorization','delivery','validity'):
                    validate_text(p[field])
            c.execute('UPDATE products SET status=?,reason=? WHERE id=?', (after, reason, pid))
            self.audit(c, actor, action, 'product', pid, reason)
            self.enqueue(c, p['seller_id'], f'Listing #{pid}: {after}. {reason}')
    def ban(self, actor, uid, banned):
        self.admin(actor)
        if uid in self.admins:
            raise ValueError('Cannot ban configured admins.')
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():
                raise ValueError('User missing.')
            c.execute('UPDATE users SET banned=? WHERE id=?',(int(banned),uid))
            if banned:
                c.execute('UPDATE products SET status="paused" WHERE seller_id=? AND status="approved"',(uid,))
            self.audit(c, actor, 'ban' if banned else 'unban', 'user', uid)
