from django.contrib.auth.models import Group
from django.utils import timezone
from django.test import TestCase
from django.core import mail
from boxes.tests import create_and_login_user
from hawkpost import celery_app
from .models import Notification, User
from .forms import UpdateUserInfoForm
from .tasks import enqueue_email_notifications
from .utils import key_state, with_gpg_obj
from .test_constants import VALID_KEY_FINGERPRINT, VALID_KEYSERVER_URL
from .test_constants import EXPIRED_KEY_FINGERPRINT
from .test_constants import REVOKED_KEY, EXPIRED_KEY, VALID_KEY

from copy import copy
from shutil import rmtree
import gnupg
import tempfile

def create_notification(sent=False, group=None):
    sent_at = timezone.now() if sent else None
    return Notification.objects.create(subject="Test subject",
                                       body="Test Body",
                                       sent_at=sent_at,
                                       send_to=group)

@with_gpg_obj
def create_expiring_key(days_to_expire, gpg):
    days_to_expire = str(days_to_expire) + "d"
    # Example values for expire_date: “2009-12-31”, “365d”, “3m”, “6w”, “5y”, “seconds=<epoch>”, 0
    input_data = gpg.gen_key_input(key_type="RSA",
                                    key_length=1024,
                                    expire_date=days_to_expire,
                                    passphrase="secret")
    key_id = gpg.gen_key(input_data)
    # retrieve the key
    key_ascii = gpg.export_keys(key_id)
    # remove the keyring
    return key_ascii

class UpdateUserFormTests(TestCase):

    def setUp(self):
        self.default_data = {
            "first_name": "some name",
            "last_name": "some last name",
            "company": "some company",
            "fingerprint": VALID_KEY_FINGERPRINT,
            "timezone": "UTC",
            "language": "en-us",
            "public_key": VALID_KEY
        }

    def test_empty_fingerprint(self):
        data = copy(self.default_data)
        data["fingerprint"] = ""
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), False)

    def test_fingerprint_plus_public_key(self):
        data = copy(self.default_data)
        data["fingerprint"] = VALID_KEY_FINGERPRINT
        data["public_key"] = VALID_KEY
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), True)

    def test_fingerprint_plus_keyserver_url(self):
        data = copy(self.default_data)
        data["keyserver_url"] = VALID_KEYSERVER_URL
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), True)

    def test_fingerprint_mismatch(self):
        data = copy(self.default_data)
        data["fingerprint"] = EXPIRED_KEY_FINGERPRINT
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), False)

    def test_empty_language(self):
        data = copy(self.default_data)
        data["language"] = ""
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), False)

    def test_non_valid_language(self):
        data = copy(self.default_data)
        data["language"] = "invalid"
        form = UpdateUserInfoForm(data)
        self.assertEqual(form.is_valid(), False)

    def test_wrong_current_password(self):
        """
        Tests if the form is invalidated because the wrong password was sent
        """
        data = {
            'current_password': 'wrongpassword',
            'timezone': 'UTC',
            'language': 'en-us'
        }
        user = create_and_login_user(self.client)
        form = UpdateUserInfoForm(data, instance=user)
        self.assertEqual(form.is_valid(), False)
        self.assertTrue('current_password' in form.errors)

    def test_invalid_password(self):
        """
        Tests that Django password constraints are being tested
        """
        data = {
            'current_password': '123123',
            'new_password1': 'a',
            'new_password2': 'a',
            'timezone': 'UTC',
            'language': 'en-us'
        }
        user = create_and_login_user(self.client)
        user.set_password('123123')
        user.save()

        form = UpdateUserInfoForm(data, instance=user)
        self.assertEqual(form.is_valid(), False)
        self.assertTrue('new_password2' in form.errors)

    def test_non_matching_passwords(self):
        """
        Tests if the form invalidates when password are valid but different
        """
        data = {
            'current_password': '123123',
            'new_password1': 'abcABCD123',
            'new_password2': 'abcABCD1234',
            'timezone': 'UTC',
            'language': 'en-us'
        }
        user = create_and_login_user(self.client)
        user.set_password('123123')
        user.save()

        form = UpdateUserInfoForm(data, instance=user)
        self.assertEqual(form.is_valid(), False)
        self.assertTrue('new_password2' in form.errors)

    def test_change_password(self):
        """
        Tests if the password is actually changed
        """
        data = {
            'current_password':'123123',
            'new_password1': 'abcABCD123',
            'new_password2': 'abcABCD123',
            'timezone': 'UTC',
            'language': 'en-us'
        }
        user = create_and_login_user(self.client)
        user.set_password('123123')
        user.save()

        form = UpdateUserInfoForm(data, instance=user)
        self.assertEqual(form.is_valid(), True)
        form.save()
        user.refresh_from_db()
        self.assertTrue(user.check_password(data["new_password1"]))


class UtilsTests(TestCase):

    def test_invalid_key_state(self):
        fingerprint, *state = key_state("invalid stuff")
        self.assertEqual(state[0], "invalid")

    def test_expired_key_state(self):
        fingerprint, *state = key_state(EXPIRED_KEY)
        self.assertEqual(state[0], "expired")

    def test_revoked_key_state(self):
        fingerprint, *state = key_state(REVOKED_KEY)
        self.assertEqual(state[0], "revoked")

    def test_valid_key_state(self):
        fingerprint, *state = key_state(VALID_KEY)
        self.assertEqual(state[0], "valid")

    def test_key_days_to_expire(self):
        key = create_expiring_key(7)
        fingerprint, *state = key_state(key)
        self.assertEqual(state[0], "valid")
        self.assertGreaterEqual(state[1], 6)
        self.assertLess(state[1], 8)

        key = create_expiring_key(1)
        fingerprint, *state = key_state(key)
        self.assertEqual(state[0], "valid")
        self.assertGreaterEqual(state[1], 0)
        self.assertLess(state[1], 1)


class UserModelTests(TestCase):

    def test_no_setup_complete(self):
        user = create_and_login_user(self.client)
        self.assertEqual(user.has_setup_complete(), False)

    def test_setup_complete(self):
        user = create_and_login_user(self.client)
        user.public_key = VALID_KEY
        user.fingerprint = VALID_KEY_FINGERPRINT
        user.save()
        self.assertEqual(user.has_setup_complete(), True)


class NotificationsTests(TestCase):

    def setUp(self):
        celery_app.conf.update(task_always_eager=True)

    def test_delete_sent_notifications(self):
        notification = create_notification(sent=True)
        notification_id = notification.id
        self.assertEqual(notification.delete(), False)
        queryset = Notification.objects.filter(id=notification_id)
        self.assertEqual(len(queryset), 1)

    def test_delete_unsent_notification(self):
        notification = create_notification(sent=False)
        notification_id = notification.id
        self.assertNotEqual(notification.delete(), False)
        queryset = Notification.objects.filter(id=notification_id)
        self.assertEqual(len(queryset), 0)

    def test_send_when_group_is_defined(self):
        for i in range(4):
            create_and_login_user(self.client)
        last_user = create_and_login_user(self.client)
        group = Group.objects.create(name="Test Group")
        group.user_set.add(last_user)
        notification = create_notification(sent=False, group=group)
        enqueue_email_notifications(notification.id, notification.send_to.id)
        self.assertEqual(len(mail.outbox), 1)

    def test_send_when_group_is_not_defined(self):
        for i in range(4):
            create_and_login_user(self.client)
        notification = create_notification(sent=False)
        enqueue_email_notifications(notification.id, None)
        self.assertEqual(len(mail.outbox), User.objects.count())


class KeyChangeRecordsTests(TestCase):

    def setUp(self):
        self.user = create_and_login_user(self.client)

        self.data = {
            'public_key': VALID_KEY,
            'fingerprint': VALID_KEY_FINGERPRINT
        }

    def test_if_no_key_change_no_record(self):
        form = UpdateUserInfoForm({}, instance=self.user)
        form.is_valid()
        form.save()
        self.assertEqual(self.user.keychanges.count(), 0)

    def test_key_changes_are_recorded(self):
        form = UpdateUserInfoForm(self.data, instance=self.user)
        form.is_valid()
        form.save()
        self.assertEqual(self.user.keychanges.count(), 1)
        keychangerecord = self.user.keychanges.last()
        self.assertEqual(keychangerecord.ip_address, None)
        self.assertEqual(keychangerecord.agent, '')

    def test_ip_address_and_user_agent_are_recorded_when_available(self):
        form = UpdateUserInfoForm(self.data, instance=self.user)
        form.is_valid()
        form.save(ip='127.0.0.1', agent='test_agent')
        self.assertEqual(self.user.keychanges.count(), 1)
        keychangerecord = self.user.keychanges.last()
        self.assertEqual(keychangerecord.ip_address, '127.0.0.1')
        self.assertEqual(keychangerecord.agent, 'test_agent')
