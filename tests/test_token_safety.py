import unittest
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models.customer import CustomerAccount, CustomerDevice, WalletTransaction
from app.models.settings import SiteSetting
from app.services.customers import create_device_link_code, link_device_with_code
from app.models.package import Package
from app.models.session import AccessSession
from app.models.token import AwardRule, AwardGrant, FreeTrialClaim, PackagePurchaseAttempt
from app.services.tokens import (
    credit_tokens, debit_tokens, claim_daily_trial,
    evaluate_consecutive_purchase_awards, purchase_token_package,
    recover_package_purchase_attempt,
)


class TokenSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class TestConfig:
            SECRET_KEY = "unit-test-secret"
            SQLALCHEMY_DATABASE_URI = "sqlite:////tmp/perfect-portal-token-unit-tests.db"
            SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 10, "check_same_thread": False}}
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


    def test_concurrent_debits_cannot_overdraw_wallet(self):
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def compete(reference):
            try:
                with self.app.app_context():
                    barrier.wait(timeout=5)
                    debit_tokens(self.account_id, 60, note="concurrent purchase", reference=reference)
                    result = "success"
            except ValueError:
                result = "insufficient"
            except Exception as exc:
                result = f"error:{type(exc).__name__}:{exc}"
            with outcome_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=compete, args=("race:thread-1",)),
            threading.Thread(target=compete, args=("race:thread-2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads), "Concurrent debit test timed out")
        self.assertEqual(outcomes.count("success"), 1, outcomes)
        self.assertEqual(outcomes.count("insufficient"), 1, outcomes)
        with self.app.app_context():
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 40)


    def test_interrupted_debit_without_session_is_refunded_once(self):
        with self.app.app_context():
            attempt = PackagePurchaseAttempt(
                account_id=self.account_id, device_id=self.device_id,
                package_id=self.package_id, package_name="Daily", token_cost=50,
                mac_address="AA:BB:CC:DD:EE:FF", ip_address="192.168.3.10",
                idempotency_key=f"{self.account_id}:recovery-no-session", status="debited",
            )
            db.session.add(attempt)
            db.session.commit()
            debit_tokens(self.account_id, 50, note="Daily", reference=f"pkg:{self.account_id}:recovery-no-session")
            attempt = db.session.get(PackagePurchaseAttempt, attempt.id)
            self.assertEqual(recover_package_purchase_attempt(attempt), "refunded")
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 100)
            self.assertEqual(WalletTransaction.query.filter_by(reference=f"refund:pkg:{self.account_id}:recovery-no-session").count(), 1)

    def test_recovery_completes_when_session_was_created_before_crash(self):
        with self.app.app_context():
            SiteSetting.set("purchasing_mode", "tokens")
            attempt = PackagePurchaseAttempt(
                account_id=self.account_id, device_id=self.device_id,
                package_id=self.package_id, package_name="Daily", token_cost=50,
                mac_address="AA:BB:CC:DD:EE:FF", ip_address="192.168.3.10",
                idempotency_key=f"{self.account_id}:crash-after-session", status="activating",
            )
            db.session.add(attempt)
            db.session.commit()
            debit_tokens(self.account_id, 50, note="Daily", reference=f"pkg:{attempt.idempotency_key}")
            from app.services.access import grant_access
            session_row = grant_access(
                package=db.session.get(Package, self.package_id),
                mac="AA:BB:CC:DD:EE:FF", ip="192.168.3.10",
                purchase_attempt_id=attempt.id,
            )
            attempt_id = attempt.id
            self.assertEqual(session_row.purchase_attempt_id, attempt_id)
            # Simulate process death after session commit but before attempt completion.
            attempt.status = "recovery_required"
            db.session.commit()
            attempt = db.session.get(PackagePurchaseAttempt, attempt_id)
            self.assertEqual(recover_package_purchase_attempt(attempt), "completed")
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 50)
            self.assertEqual(PackagePurchaseAttempt.query.filter_by(id=attempt_id).one().status, "completed")

    def test_gateway_authorisation_failure_revokes_then_refunds(self):
        with self.app.app_context():
            SiteSetting.set("purchasing_mode", "tokens")
            self.app.config["GATEWAY_ENABLED"] = True
            from app.services import access as access_service
            with patch.object(access_service, "run_gateway", side_effect=lambda action, *args: action == "revoke"):
                with self.assertRaises(ValueError):
                    purchase_token_package(
                        db.session.get(CustomerAccount, self.account_id),
                        db.session.get(CustomerDevice, self.device_id),
                        db.session.get(Package, self.package_id), "192.168.3.10",
                        "gateway-failure-test",
                    )
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 100)
            attempt = PackagePurchaseAttempt.query.filter_by(idempotency_key=f"{self.account_id}:gateway-failure-test").one()
            self.assertEqual(attempt.status, "refunded")

    def test_duplicate_purchase_request_does_not_debit_twice(self):
        with self.app.app_context():
            SiteSetting.set("purchasing_mode", "tokens")
            device = db.session.get(CustomerDevice, self.device_id)
            account = db.session.get(CustomerAccount, self.account_id)
            package = db.session.get(Package, self.package_id)
            first = purchase_token_package(account, device, package, "192.168.3.10", "same-request")
            self.assertTrue(first.is_active())
            with self.assertRaises(ValueError):
                purchase_token_package(account, device, package, "192.168.3.10", "same-request")
            self.assertEqual(db.session.get(CustomerAccount, self.account_id).token_balance, 50)
            self.assertEqual(PackagePurchaseAttempt.query.filter_by(idempotency_key=f"{self.account_id}:same-request").count(), 1)

    def test_device_link_code_cannot_be_reused(self):
        with self.app.app_context():
            account = db.session.get(CustomerAccount, self.account_id)
            code = create_device_link_code(account)
            linked, device = link_device_with_code(code, "12:34:56:78:9A:BC", "192.168.3.11")
            self.assertEqual(linked.id, self.account_id)
            self.assertEqual(device.mac_address, "12:34:56:78:9A:BC")
            with self.assertRaises(ValueError):
                link_device_with_code(code, "12:34:56:78:9A:BD", "192.168.3.12")

    def test_trial_activation_failure_releases_daily_claim(self):
        with self.app.app_context():
            SiteSetting.set("purchasing_mode", "tokens")
        from app.blueprints.portal import routes as portal_routes
        from app.services import access as access_service
        client = self.app.test_client()
        with patch.object(portal_routes, "_client_mac", return_value="AA:BB:CC:DD:EE:FF"), \
             patch.object(portal_routes, "_client_ip", return_value="192.168.3.10"), \
             patch.object(access_service, "grant_access", side_effect=RuntimeError("simulated activation crash")):
            response = client.post("/tokens/trial")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(FreeTrialClaim.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
