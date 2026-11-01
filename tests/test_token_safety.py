import unittest
from datetime import datetime, timezone

from app import create_app
from app.extensions import db
from app.models.customer import CustomerAccount, CustomerDevice, WalletTransaction
from app.models.package import Package
from app.models.session import AccessSession
from app.models.token import AwardRule, AwardGrant, FreeTrialClaim
from app.services.tokens import (
    credit_tokens, debit_tokens, claim_daily_trial,
    evaluate_consecutive_purchase_awards,
)


class TokenSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class TestConfig:
            SECRET_KEY = "unit-test-secret"
            SQLALCHEMY_DATABASE_URI = "sqlite://"
            SQLALCHEMY_TRACK_MODIFICATIONS = False
            WTF_CSRF_ENABLED = False
            GATEWAY_ENABLED = False
            GATEWAY_SCRIPT = ""
            UPLOAD_FOLDER = "/tmp/perfect-portal-test-uploads"
            ADMIN_USERNAME = "test-admin"
            ADMIN_PASSWORD = "test-password"
            DISPLAY_TIMEZONE = "Africa/Dar_es_Salaam"

        cls.app = create_app(TestConfig)
        cls.app.testing = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            self.account = CustomerAccount(username="USER12345", token_balance=100)
            db.session.add(self.account)
            db.session.flush()
            self.device = CustomerDevice(
                account_id=self.account.id, mac_address="AA:BB:CC:DD:EE:FF",
                last_ip_address="192.168.3.10",
            )
            self.package = Package(
                name="Daily", description="", price_tzs=500, token_cost=50,
                duration_minutes=1440, sort_order=1, is_active=True,
            )
            self.other_package = Package(
                name="Hourly", description="", price_tzs=200, token_cost=20,
                duration_minutes=60, sort_order=2, is_active=True,
            )
            db.session.add_all([self.device, self.package, self.other_package])
            db.session.commit()
            self.account_id = self.account.id
            self.device_id = self.device.id
            self.package_id = self.package.id
            self.other_package_id = self.other_package.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_wallet_debit_is_atomic_and_rejects_insufficient_balance(self):
        with self.app.app_context():
            debit_tokens(self.account_id, 30, note="test purchase", reference="test:debit")
            account = db.session.get(CustomerAccount, self.account_id)
            self.assertEqual(account.token_balance, 70)
            with self.assertRaises(ValueError):
                debit_tokens(self.account_id, 100, note="too much", reference="test:overspend")
            db.session.refresh(account)
            self.assertEqual(account.token_balance, 70)

    def test_wallet_reference_is_idempotent(self):
        with self.app.app_context():
            credit_tokens(self.account_id, 10, reference="test:credit")
            credit_tokens(self.account_id, 10, reference="test:credit")
            account = db.session.get(CustomerAccount, self.account_id)
            self.assertEqual(account.token_balance, 110)
            self.assertEqual(WalletTransaction.query.filter_by(reference="test:credit").count(), 1)

    def test_daily_trial_is_unique_per_account(self):
        with self.app.app_context():
            claim_daily_trial(self.account_id, self.device_id)
            with self.assertRaises(ValueError):
                claim_daily_trial(self.account_id, self.device_id)
            self.assertEqual(FreeTrialClaim.query.filter_by(account_id=self.account_id).count(), 1)

    def _session(self, day, package_id):
        # 12:00 UTC is 15:00 in Dar es Salaam.
        stamp = datetime.fromisoformat(day + "T12:00:00+00:00")
        return AccessSession(
            mac_address="AA:BB:CC:DD:EE:FF", ip_address="192.168.3.10",
            package_id=package_id, started_at=stamp,
            expires_at=datetime.fromtimestamp(stamp.timestamp() + 3600, timezone.utc),
            started_at_unix=int(stamp.timestamp()),
            expires_at_unix=int(stamp.timestamp()) + 3600,
            revoked=True,
        )

    def test_consecutive_award_counts_historical_sessions_and_grants_once(self):
        with self.app.app_context():
            rule = AwardRule(
                name="Three daily purchases", rule_type="consecutive_package_days",
                criteria_json='{"consecutive_days": 3, "package_id": %d}' % self.package_id,
                reward_type="tokens", reward_amount=15, repeatable=False,
            )
            db.session.add(rule)
            db.session.add_all([
                self._session("2026-10-28", self.package_id),
                self._session("2026-10-29", self.package_id),
                self._session("2026-10-30", self.package_id),
            ])
            db.session.commit()
            current = AccessSession.query.order_by(AccessSession.started_at.desc()).first()
            evaluate_consecutive_purchase_awards(db.session.get(CustomerAccount, self.account_id), current)
            evaluate_consecutive_purchase_awards(db.session.get(CustomerAccount, self.account_id), current)
            account = db.session.get(CustomerAccount, self.account_id)
            self.assertEqual(account.token_balance, 115)
            self.assertEqual(AwardGrant.query.filter_by(rule_id=rule.id).count(), 1)

    def test_different_package_breaks_same_package_streak(self):
        with self.app.app_context():
            rule = AwardRule(
                name="Three daily purchases", rule_type="consecutive_package_days",
                criteria_json='{"consecutive_days": 3, "package_id": %d}' % self.package_id,
                reward_type="tokens", reward_amount=15, repeatable=False,
            )
            db.session.add(rule)
            db.session.add_all([
                self._session("2026-10-28", self.package_id),
                self._session("2026-10-29", self.package_id),
                self._session("2026-10-29", self.other_package_id),
                self._session("2026-10-30", self.package_id),
            ])
            db.session.commit()
            current = AccessSession.query.order_by(AccessSession.started_at.desc()).first()
            evaluate_consecutive_purchase_awards(db.session.get(CustomerAccount, self.account_id), current)
            self.assertEqual(AwardGrant.query.filter_by(rule_id=rule.id).count(), 0)
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 100)


if __name__ == "__main__":
    unittest.main()
