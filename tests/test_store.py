import tempfile
import unittest
from pathlib import Path

from marketplace.db import Store
from marketplace.policy import buyer_cents, money, price_cents, validate_text


def product(quantity=5):
    return dict(name='Design service', category='General', description='Product listing: Design service',
                authorization='Seller confirmed the product details for admin review.',
                delivery='24', validity='Not specified', seller_price=40000,
                total=quantity, negotiable=True)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name) / 'test.sqlite3', {1})
        for uid in range(1, 5):
            self.db.register(uid, f'user{uid}')
        self.db.set_user(2, 'seller', True)

    def tearDown(self):
        self.tmp.cleanup()

    def listing(self, approved=True):
        pid = self.db.submit(2, product())
        if approved:
            self.db.moderate(1, pid, 'approve')
        return pid

    def test_markup_and_money_validation(self):
        self.assertEqual(buyer_cents(40000), 44000)
        self.assertEqual(buyer_cents(5), 6)
        self.assertEqual(money(44000, 'USD'), '$440.00')
        self.assertEqual(money(44000, 'EUR'), 'EUR 440.00')
        self.assertEqual(price_cents('0.01'), 1)
        for text in ('NaN', 'Infinity', '-1', '0', '1.001', '1000001'):
            with self.assertRaises(ValueError):
                price_cents(text)

    def test_pending_listing_is_private_until_approval(self):
        pid = self.listing(approved=False)
        self.assertEqual(self.db.product(pid)['status'], 'pending')
        with self.assertRaises(ValueError):
            self.db.moderate(3, pid, 'approve')
        self.db.moderate(1, pid, 'approve')
        public = self.db.public_text(self.db.product(pid))
        self.assertIn('$440.00', public)
        self.assertIn('Available stock: 5', public)
        self.assertIn('Negotiable: Yes', public)
        self.assertNotIn('400.00', public)
        self.assertNotIn('resale permission', public)

    def test_buyer_interest_notifies_admin_without_creating_order(self):
        pid = self.listing()
        selected = self.db.contact_admin(3, pid)
        self.assertEqual(selected['id'], pid)
        self.assertIsNone(self.db.one('SELECT * FROM orders'))
        notice = self.db.one('SELECT body FROM outbox WHERE chat_id=1 ORDER BY id DESC')
        self.assertIn('Buyer 3 is interested', notice['body'])

    def test_edit_requires_fresh_approval_and_preserves_owner(self):
        pid = self.listing()
        updated = product()
        updated.update(name='Updated product', total=8, seller_price=50000, negotiable=False)
        self.db.submit(2, updated, pid)
        self.assertEqual(self.db.product(pid)['status'], 'pending')
        self.assertEqual(self.db.product(pid)['revision'], 2)
        self.assertEqual(self.db.product(pid)['total'], 8)
        with self.assertRaises(ValueError):
            self.db.submit(3, updated, pid)

    def test_ban_pauses_approved_products(self):
        pid = self.listing()
        self.db.ban(1, 2, True)
        self.assertEqual(self.db.product(pid)['status'], 'paused')
        with self.assertRaises(ValueError):
            self.db.contact_admin(3, pid)
        self.db.ban(1, 2, False)

    def test_policy_rejection(self):
        for text in ('live API key', 'OpenAI accounts', 'non-transferable credits',
                     'trial credits', 'sk-' + 'a' * 30, 'AKIA' + 'A' * 16,
                     'ghp_' + 'b' * 30, 'AIza' + 'c' * 35,
                     'api token', '-----BEGIN PRIVATE KEY-----'):
            with self.assertRaises(ValueError):
                validate_text(text)


if __name__ == '__main__':
    unittest.main()