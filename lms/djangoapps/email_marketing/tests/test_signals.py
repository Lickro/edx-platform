"""Tests of email marketing signal handlers."""
import datetime
import logging

import ddt
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sites.models import Site
from django.test import TestCase
from django.test.client import RequestFactory
from freezegun import freeze_time
from mock import ANY, patch
from opaque_keys.edx.keys import CourseKey
from sailthru.sailthru_error import SailthruClientError
from sailthru.sailthru_response import SailthruResponse
from testfixtures import LogCapture

from email_marketing.models import EmailMarketingConfiguration
from email_marketing.signals import (
    add_email_marketing_cookies,
    email_marketing_register_user,
    email_marketing_user_field_changed
)
from email_marketing.tasks import (
    _create_user_list,
    _get_list_from_email_marketing_provider,
    _get_or_create_user_list,
    update_user,
    update_user_email,
    get_email_cookies_via_sailthru
)
from openedx.core.djangoapps.lang_pref import LANGUAGE_KEY
from student.tests.factories import UserFactory, UserProfileFactory
from util.json_request import JsonResponse

log = logging.getLogger(__name__)

LOGGER_NAME = "email_marketing.signals"

TEST_EMAIL = "test@edx.org"


def update_email_marketing_config(enabled=True, key='badkey', secret='badsecret', new_user_list='new list',
                                  template='Welcome', enroll_cost=100, lms_url_override='http://testserver'):
    """
    Enable / Disable Sailthru integration
    """
    return EmailMarketingConfiguration.objects.create(
        enabled=enabled,
        sailthru_key=key,
        sailthru_secret=secret,
        sailthru_new_user_list=new_user_list,
        sailthru_welcome_template=template,
        sailthru_enroll_template='enroll_template',
        sailthru_lms_url_override=lms_url_override,
        sailthru_get_tags_from_sailthru=False,
        sailthru_enroll_cost=enroll_cost,
        sailthru_max_retries=0,
        welcome_email_send_delay=600
    )


@ddt.ddt
class EmailMarketingTests(TestCase):
    """
    Tests for the EmailMarketing signals and tasks classes.
    """

    def setUp(self):
        update_email_marketing_config(enabled=False)
        self.request_factory = RequestFactory()
        self.user = UserFactory.create(username='test', email=TEST_EMAIL)
        self.profile = self.user.profile
        self.profile.year_of_birth = 1980
        self.profile.save()

        self.request = self.request_factory.get("foo")
        update_email_marketing_config(enabled=True)

        # create some test course objects
        self.course_id_string = 'edX/toy/2012_Fall'
        self.course_id = CourseKey.from_string(self.course_id_string)
        self.course_url = 'http://testserver/courses/edX/toy/2012_Fall/info'

        self.site = Site.objects.get_current()
        self.site_domain = self.site.domain
        self.request.site = self.site
        super(EmailMarketingTests, self).setUp()

    @freeze_time(datetime.datetime.now())
    @patch('email_marketing.signals.crum.get_current_request')
    @patch('email_marketing.signals.SailthruClient.api_post')
    def test_drop_cookie(self, mock_sailthru, mock_get_current_request):
        """
        Test add_email_marketing_cookies
        """
        response = JsonResponse({
            "success": True,
            "redirect_url": 'test.com/test',
        })
        self.request.COOKIES['anonymous_interest'] = 'cookie_content'
        mock_get_current_request.return_value = self.request

        cookies = {'cookie': 'test_cookie'}
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'keys': cookies}))

        with LogCapture(LOGGER_NAME, level=logging.INFO) as logger:
            add_email_marketing_cookies(None, response=response, user=self.user)
            logger.check(
                (LOGGER_NAME, 'INFO',
                    'Started at {start} and ended at {end}, time spent:{delta} milliseconds'.format(
                        start=datetime.datetime.now().isoformat(' '),
                        end=datetime.datetime.now().isoformat(' '),
                        delta=0)
                 ),
                (LOGGER_NAME, 'INFO',
                    'sailthru_hid cookie:{cookies[cookie]} successfully retrieved for user {user}'.format(
                        cookies=cookies,
                        user=TEST_EMAIL)
                 )
            )
        mock_sailthru.assert_called_with('user',
                                         {'fields': {'keys': 1},
                                          'cookies': {'anonymous_interest': 'cookie_content'},
                                          'id': TEST_EMAIL,
                                          'vars': {'last_login_date': ANY}})
        self.assertTrue('sailthru_hid' in response.cookies)
        self.assertEquals(response.cookies['sailthru_hid'].value, "test_cookie")

    @patch('email_marketing.signals.SailthruClient.api_post')
    def test_get_cookies_via_sailthu(self, mock_sailthru):

        cookies = {'cookie': 'test_cookie'}
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'keys': cookies}))

        post_parms = {
            'id': self.user.email,
            'fields': {'keys': 1},
            'vars': {'last_login_date': datetime.datetime.now().strftime("%Y-%m-%d")},
            'cookies': {'anonymous_interest': 'cookie_content'}
        }
        expected_cookie = get_email_cookies_via_sailthru.delay(self.user.email, post_parms)

        mock_sailthru.assert_called_with('user',
                                         {'fields': {'keys': 1},
                                          'cookies': {'anonymous_interest': 'cookie_content'},
                                          'id': TEST_EMAIL,
                                          'vars': {'last_login_date': ANY}})

        self.assertEqual(cookies['cookie'], expected_cookie.result)

    @patch('email_marketing.signals.SailthruClient.api_post')
    def test_drop_cookie_error_path(self, mock_sailthru):
        """
        test that error paths return no cookie
        """
        response = JsonResponse({
            "success": True,
            "redirect_url": 'test.com/test',
        })
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'keys': {'cookiexx': 'test_cookie'}}))
        add_email_marketing_cookies(None, response=response, user=self.user)
        self.assertFalse('sailthru_hid' in response.cookies)

        mock_sailthru.return_value = SailthruResponse(JsonResponse({'error': "error", "errormsg": "errormsg"}))
        add_email_marketing_cookies(None, response=response, user=self.user)
        self.assertFalse('sailthru_hid' in response.cookies)

        mock_sailthru.side_effect = SailthruClientError
        add_email_marketing_cookies(None, response=response, user=self.user)
        self.assertFalse('sailthru_hid' in response.cookies)

    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    @patch('email_marketing.tasks.SailthruClient.api_get')
    def test_add_user(self, mock_sailthru_get, mock_sailthru_post, mock_log_error):
        """
        test async method in tasks that actually updates Sailthru and send Welcome template.
        """
        site_dict = {'id': self.site.id, 'domain': self.site.domain, 'name': self.site.name}
        mock_sailthru_post.return_value = SailthruResponse(JsonResponse({'ok': True}))
        mock_sailthru_get.return_value = SailthruResponse(JsonResponse({'lists': [{'name': 'new list'}], 'ok': True}))
        update_user.delay(
            {'gender': 'm', 'username': 'test', 'activated': 1}, TEST_EMAIL, site_dict, new_user=True
        )
        expected_schedule = datetime.datetime.utcnow() + datetime.timedelta(seconds=600)
        self.assertFalse(mock_log_error.called)
        self.assertEquals(mock_sailthru_post.call_args[0][0], "send")
        userparms = mock_sailthru_post.call_args[0][1]
        self.assertEquals(userparms['email'], TEST_EMAIL)
        self.assertEquals(userparms['template'], "Welcome")
        self.assertEquals(userparms['schedule_time'], expected_schedule.strftime('%Y-%m-%dT%H:%M:%SZ'))

    @patch('email_marketing.tasks.SailthruClient.api_post')
    @patch('email_marketing.tasks.SailthruClient.api_get')
    def test_add_user_list_existing_domain(self, mock_sailthru_get, mock_sailthru_post):
        """
        test non existing domain name updates Sailthru user lists with default list
        """
        # Set template to empty string to disable 2nd post call to Sailthru
        update_email_marketing_config(template='')
        existing_site = Site.objects.create(domain='testing.com', name='testing.com')
        site_dict = {'id': existing_site.id, 'domain': existing_site.domain, 'name': existing_site.name}
        mock_sailthru_post.return_value = SailthruResponse(JsonResponse({'ok': True}))
        mock_sailthru_get.return_value = SailthruResponse(
            JsonResponse({'lists': [{'name': 'new list'}, {'name': 'testing_com_user_list'}], 'ok': True})
        )
        update_user.delay(
            {'gender': 'm', 'username': 'test', 'activated': 1}, TEST_EMAIL, site=site_dict, new_user=True
        )
        self.assertEquals(mock_sailthru_post.call_args[0][0], "user")
        userparms = mock_sailthru_post.call_args[0][1]
        self.assertEquals(userparms['lists']['testing_com_user_list'], 1)

    @patch('email_marketing.tasks.SailthruClient.api_post')
    @patch('email_marketing.tasks.SailthruClient.api_get')
    def test_user_activation(self, mock_sailthru_get, mock_sailthru_post):
        """
        Test that welcome template not sent if not new user.
        """
        mock_sailthru_post.return_value = SailthruResponse(JsonResponse({'ok': True}))
        mock_sailthru_get.return_value = SailthruResponse(JsonResponse({'lists': [{'name': 'new list'}], 'ok': True}))
        update_user.delay({}, self.user.email, new_user=False)
        # look for call args for 2nd call
        self.assertEquals(mock_sailthru_post.call_args[0][0], "user")
        userparms = mock_sailthru_post.call_args[0][1]
        self.assertIsNone(userparms.get('email'))
        self.assertIsNone(userparms.get('template'))

    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_update_user_error_logging(self, mock_sailthru, mock_log_error):
        """
        Ensure that error returned from Sailthru api is logged
        """
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'error': 100, 'errormsg': 'Got an error'}))
        update_user.delay({}, self.user.email)
        self.assertTrue(mock_log_error.called)

        # force Sailthru API exception
        mock_log_error.reset_mock()
        mock_sailthru.side_effect = SailthruClientError
        update_user.delay({}, self.user.email, self.site_domain)
        self.assertTrue(mock_log_error.called)

        # force Sailthru API exception on 2nd call
        mock_log_error.reset_mock()
        mock_sailthru.side_effect = [SailthruResponse(JsonResponse({'ok': True})), SailthruClientError]
        update_user.delay({}, self.user.email, new_user=True)
        self.assertTrue(mock_log_error.called)

        # force Sailthru API error return on 2nd call
        mock_log_error.reset_mock()
        mock_sailthru.side_effect = [SailthruResponse(JsonResponse({'ok': True})),
                                     SailthruResponse(JsonResponse({'error': 100, 'errormsg': 'Got an error'}))]
        update_user.delay({}, self.user.email, new_user=True)
        self.assertTrue(mock_log_error.called)

    @patch('email_marketing.tasks.update_user.retry')
    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_update_user_error_retryable(self, mock_sailthru, mock_log_error, mock_retry):
        """
        Ensure that retryable error is retried
        """
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'error': 43, 'errormsg': 'Got an error'}))
        update_user.delay({}, self.user.email)
        self.assertTrue(mock_log_error.called)
        self.assertTrue(mock_retry.called)

    @patch('email_marketing.tasks.update_user.retry')
    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_update_user_error_nonretryable(self, mock_sailthru, mock_log_error, mock_retry):
        """
        Ensure that non-retryable error is not retried
        """
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'error': 1, 'errormsg': 'Got an error'}))
        update_user.delay({}, self.user.email)
        self.assertTrue(mock_log_error.called)
        self.assertFalse(mock_retry.called)

    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_just_return_tasks(self, mock_sailthru, mock_log_error):
        """
        Ensure that disabling Sailthru just returns
        """
        update_email_marketing_config(enabled=False)

        update_user.delay(self.user.username)
        self.assertFalse(mock_log_error.called)
        self.assertFalse(mock_sailthru.called)

        update_user_email.delay(self.user.username, "newemail2@test.com")
        self.assertFalse(mock_log_error.called)
        self.assertFalse(mock_sailthru.called)

        update_email_marketing_config(enabled=True)

    @patch('email_marketing.signals.log.error')
    def test_just_return_signals(self, mock_log_error):
        """
        Ensure that disabling Sailthru just returns
        """
        update_email_marketing_config(enabled=False)

        add_email_marketing_cookies(None)
        self.assertFalse(mock_log_error.called)

        email_marketing_register_user(None)
        self.assertFalse(mock_log_error.called)

        update_email_marketing_config(enabled=True)

        # test anonymous users
        anon = AnonymousUser()
        email_marketing_register_user(None, user=anon)
        self.assertFalse(mock_log_error.called)

        email_marketing_user_field_changed(None, user=anon)
        self.assertFalse(mock_log_error.called)

    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_change_email(self, mock_sailthru):
        """
        test async method in task that changes email in Sailthru
        """
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'ok': True}))
        update_user_email.delay(TEST_EMAIL, "old@edx.org")
        self.assertEquals(mock_sailthru.call_args[0][0], "user")
        userparms = mock_sailthru.call_args[0][1]
        self.assertEquals(userparms['key'], "email")
        self.assertEquals(userparms['id'], "old@edx.org")
        self.assertEquals(userparms['keys']['email'], TEST_EMAIL)

    @patch('email_marketing.tasks.SailthruClient')
    def test_get_or_create_sailthru_list(self, mock_sailthru_client):
        """
        Test the task the create sailthru lists.
        """
        mock_sailthru_client.api_get.return_value = SailthruResponse(JsonResponse({'lists': []}))
        _get_or_create_user_list(mock_sailthru_client, 'test1_user_list')
        mock_sailthru_client.api_get.assert_called_with("list", {})
        mock_sailthru_client.api_post.assert_called_with(
            "list", {'list': 'test1_user_list', 'primary': 0, 'public_name': 'test1_user_list'}
        )

        # test existing user list
        mock_sailthru_client.api_get.return_value = \
            SailthruResponse(JsonResponse({'lists': [{'name': 'test1_user_list'}]}))
        _get_or_create_user_list(mock_sailthru_client, 'test2_user_list')
        mock_sailthru_client.api_get.assert_called_with("list", {})
        mock_sailthru_client.api_post.assert_called_with(
            "list", {'list': 'test2_user_list', 'primary': 0, 'public_name': 'test2_user_list'}
        )

        # test get error from Sailthru
        mock_sailthru_client.api_get.return_value = \
            SailthruResponse(JsonResponse({'error': 43, 'errormsg': 'Got an error'}))
        self.assertEqual(_get_or_create_user_list(
            mock_sailthru_client, 'test1_user_list'), None
        )

        # test post error from Sailthru
        mock_sailthru_client.api_post.return_value = \
            SailthruResponse(JsonResponse({'error': 43, 'errormsg': 'Got an error'}))
        mock_sailthru_client.api_get.return_value = SailthruResponse(JsonResponse({'lists': []}))
        self.assertEqual(_get_or_create_user_list(mock_sailthru_client, 'test2_user_list'), None)

    @patch('email_marketing.tasks.SailthruClient')
    def test_get_sailthru_list_map_no_list(self, mock_sailthru_client):
        """Test when no list returned from sailthru"""
        mock_sailthru_client.api_get.return_value = SailthruResponse(JsonResponse({'lists': []}))
        self.assertEqual(_get_list_from_email_marketing_provider(mock_sailthru_client), {})
        mock_sailthru_client.api_get.assert_called_with("list", {})

    @patch('email_marketing.tasks.SailthruClient')
    def test_get_sailthru_list_map_error(self, mock_sailthru_client):
        """Test when error occurred while fetching data from sailthru"""
        mock_sailthru_client.api_get.return_value = SailthruResponse(
            JsonResponse({'error': 43, 'errormsg': 'Got an error'})
        )
        self.assertEqual(_get_list_from_email_marketing_provider(mock_sailthru_client), {})

    @patch('email_marketing.tasks.SailthruClient')
    def test_get_sailthru_list_map_exception(self, mock_sailthru_client):
        """Test when exception raised while fetching data from sailthru"""
        mock_sailthru_client.api_get.side_effect = SailthruClientError
        self.assertEqual(_get_list_from_email_marketing_provider(mock_sailthru_client), {})

    @patch('email_marketing.tasks.SailthruClient')
    def test_get_sailthru_list(self, mock_sailthru_client):
        """Test fetch list data from sailthru"""
        mock_sailthru_client.api_get.return_value = \
            SailthruResponse(JsonResponse({'lists': [{'name': 'test1_user_list'}]}))
        self.assertEqual(
            _get_list_from_email_marketing_provider(mock_sailthru_client),
            {'test1_user_list': {'name': 'test1_user_list'}}
        )
        mock_sailthru_client.api_get.assert_called_with("list", {})

    @patch('email_marketing.tasks.SailthruClient')
    def test_create_sailthru_list(self, mock_sailthru_client):
        """Test create list in sailthru"""
        mock_sailthru_client.api_post.return_value = SailthruResponse(JsonResponse({'ok': True}))
        self.assertEqual(_create_user_list(mock_sailthru_client, 'test_list_name'), True)
        self.assertEquals(mock_sailthru_client.api_post.call_args[0][0], "list")
        listparms = mock_sailthru_client.api_post.call_args[0][1]
        self.assertEqual(listparms['list'], 'test_list_name')
        self.assertEqual(listparms['primary'], 0)
        self.assertEqual(listparms['public_name'], 'test_list_name')

    @patch('email_marketing.tasks.SailthruClient')
    def test_create_sailthru_list_error(self, mock_sailthru_client):
        """Test error occurrence while creating sailthru list"""
        mock_sailthru_client.api_post.return_value = SailthruResponse(
            JsonResponse({'error': 43, 'errormsg': 'Got an error'})
        )
        self.assertEqual(_create_user_list(mock_sailthru_client, 'test_list_name'), False)

    @patch('email_marketing.tasks.SailthruClient')
    def test_create_sailthru_list_exception(self, mock_sailthru_client):
        """Test exception raised while creating sailthru list"""
        mock_sailthru_client.api_post.side_effect = SailthruClientError
        self.assertEqual(_create_user_list(mock_sailthru_client, 'test_list_name'), False)

    @patch('email_marketing.tasks.log.error')
    @patch('email_marketing.tasks.SailthruClient.api_post')
    def test_error_logging(self, mock_sailthru, mock_log_error):
        """
        Ensure that error returned from Sailthru api is logged
        """
        mock_sailthru.return_value = SailthruResponse(JsonResponse({'error': 100, 'errormsg': 'Got an error'}))
        update_user_email.delay(self.user.username, "newemail2@test.com")
        self.assertTrue(mock_log_error.called)

        mock_sailthru.side_effect = SailthruClientError
        update_user_email.delay(self.user.username, "newemail2@test.com")
        self.assertTrue(mock_log_error.called)

    @patch('email_marketing.signals.crum.get_current_request')
    @patch('lms.djangoapps.email_marketing.tasks.update_user.delay')
    def test_register_user(self, mock_update_user, mock_get_current_request):
        """
        make sure register user call invokes update_user
        """
        mock_get_current_request.return_value = self.request
        email_marketing_register_user(None, user=self.user, profile=self.profile)
        self.assertTrue(mock_update_user.called)

    @patch('lms.djangoapps.email_marketing.tasks.update_user.delay')
    def test_register_user_no_request(self, mock_update_user):
        """
        make sure register user call invokes update_user
        """
        email_marketing_register_user(None, user=self.user, profile=self.profile)
        self.assertTrue(mock_update_user.called)

    @patch('lms.djangoapps.email_marketing.tasks.update_user.delay')
    def test_register_user_language_preference(self, mock_update_user):
        """
        make sure register user call invokes update_user and includes language preference
        """
        # If the user hasn't set an explicit language preference, we should send the application's default.
        self.assertIsNone(self.user.preferences.model.get_value(self.user, LANGUAGE_KEY))
        email_marketing_register_user(None, user=self.user, profile=self.profile)
        self.assertEqual(mock_update_user.call_args[0][0]['ui_lang'], settings.LANGUAGE_CODE)

        # If the user has set an explicit language preference, we should send it.
        self.user.preferences.create(key=LANGUAGE_KEY, value='es-419')
        email_marketing_register_user(None, user=self.user, profile=self.profile)
        self.assertEqual(mock_update_user.call_args[0][0]['ui_lang'], 'es-419')

    @patch('email_marketing.signals.crum.get_current_request')
    @patch('lms.djangoapps.email_marketing.tasks.update_user.delay')
    @ddt.data(('auth_userprofile', 'gender', 'f', True),
              ('auth_user', 'is_active', 1, True),
              ('auth_userprofile', 'shoe_size', 1, False),
              ('user_api_userpreference', 'pref-lang', 'en', True))
    @ddt.unpack
    def test_modify_field(self, table, setting, value, result, mock_update_user, mock_get_current_request):
        """
        Test that correct fields call update_user
        """
        mock_get_current_request.return_value = self.request
        email_marketing_user_field_changed(None, self.user, table=table, setting=setting, new_value=value)
        self.assertEqual(mock_update_user.called, result)

    @patch('lms.djangoapps.email_marketing.tasks.update_user.delay')
    def test_modify_language_preference(self, mock_update_user):
        """
        Test that update_user is called with new language preference
        """
        # If the user hasn't set an explicit language preference, we should send the application's default.
        self.assertIsNone(self.user.preferences.model.get_value(self.user, LANGUAGE_KEY))
        email_marketing_user_field_changed(
            None, self.user, table='user_api_userpreference', setting=LANGUAGE_KEY, new_value=None
        )
        self.assertEqual(mock_update_user.call_args[0][0]['ui_lang'], settings.LANGUAGE_CODE)

        # If the user has set an explicit language preference, we should send it.
        self.user.preferences.create(key=LANGUAGE_KEY, value='fr')
        email_marketing_user_field_changed(
            None, self.user, table='user_api_userpreference', setting=LANGUAGE_KEY, new_value='fr'
        )
        self.assertEqual(mock_update_user.call_args[0][0]['ui_lang'], 'fr')

    @patch('lms.djangoapps.email_marketing.tasks.update_user_email.delay')
    def test_modify_email(self, mock_update_user):
        """
        Test that change to email calls update_user_email
        """
        email_marketing_user_field_changed(None, self.user, table='auth_user', setting='email', old_value='new@a.com')
        mock_update_user.assert_called_with(self.user.email, 'new@a.com')

        # make sure nothing called if disabled
        mock_update_user.reset_mock()
        update_email_marketing_config(enabled=False)
        email_marketing_user_field_changed(None, self.user, table='auth_user', setting='email', old_value='new@a.com')
        self.assertFalse(mock_update_user.called)
