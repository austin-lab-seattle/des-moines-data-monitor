import json
import unittest
from unittest.mock import Mock

from scripts.aws.manage_research_access import change_membership


class ResearchAccessAuditTests(unittest.TestCase):
    def test_durable_intent_precedes_access_and_success_is_a_separate_record(self):
        events = []
        s3, cognito = Mock(), Mock()
        s3.put_object.side_effect = lambda **kw: events.append(('audit', kw))
        cognito.admin_add_user_to_group.side_effect = lambda **kw: events.append(('grant', kw))
        audit = change_membership(cognito, s3, 'private', {'Username': 'existing'},
                                  {'arn': 'operator'}, 'approved@example.org')
        self.assertEqual(['audit', 'grant', 'audit'], [kind for kind, _ in events])
        self.assertEqual('PENDING', json.loads(events[0][1]['Body'])['result'])
        self.assertEqual('SUCCEEDED', json.loads(events[2][1]['Body'])['result'])
        self.assertNotEqual(events[0][1]['Key'], events[2][1]['Key'])
        self.assertTrue(all(audit in event[1]['Key'] and event[1]['IfNoneMatch'] == '*'
                            for event in (events[0], events[2])))
        self.assertEqual('existing', events[1][1]['Username'])

    def test_audit_failure_prevents_membership_change(self):
        s3, cognito = Mock(), Mock()
        s3.put_object.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            change_membership(cognito, s3, 'private', {'Username': 'existing'}, {}, 'approved@example.org')
        cognito.admin_add_user_to_group.assert_not_called()

    def test_failed_revoke_is_recorded_without_claiming_success(self):
        s3, cognito = Mock(), Mock()
        cognito.admin_remove_user_from_group.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            change_membership(cognito, s3, 'private', {'Username': 'existing'}, {}, 'approved@example.org', True)
        self.assertEqual('FAILED', json.loads(s3.put_object.call_args.kwargs['Body'])['result'])
        cognito.admin_add_user_to_group.assert_not_called()
