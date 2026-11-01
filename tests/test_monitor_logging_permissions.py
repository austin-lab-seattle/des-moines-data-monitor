"""Verify deployment permissions without importing the deployment's AWS writes."""
import ast
import fnmatch
import unittest
from pathlib import Path


class MonitorLoggingPermissionsTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'scripts/aws/deploy_upload_monitor.py'
        assignment = next(node for node in ast.parse(source.read_text()).body
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'role_policy'
                                  for target in node.targets))
        settings = dict(REGION='us-west-2', ACCOUNT_ID='123456789012',
                        FUNCTION_NAME='test-monitor', BUCKET='test-data',
                        SOURCE_EMAIL='test@example.invalid')
        self.policy = eval(compile(ast.Expression(assignment.value), str(source), 'eval'),
                           {'__builtins__': {}, **settings})
        self.group = 'arn:aws:logs:us-west-2:123456789012:log-group:/aws/lambda/test-monitor'

    def grants(self, action, resource):
        # Only evaluate the unconditional logging grants under test, not IAM's
        # general policy language or the unrelated conditional S3/SES statements.
        for statement in self.policy['Statement']:
            if statement['Effect'] != 'Allow' or statement.get('Condition'):
                continue
            actions = statement['Action']
            resources = statement['Resource']
            actions = actions if isinstance(actions, list) else [actions]
            resources = resources if isinstance(resources, list) else [resources]
            if (any(fnmatch.fnmatchcase(action, item) for item in actions)
                    and any(fnmatch.fnmatchcase(resource, item) for item in resources)):
                return True
        return False

    def test_group_creation_uses_group_arn_and_stream_writes_remain_allowed(self):
        self.assertTrue(self.grants('logs:CreateLogGroup', self.group))
        stream = self.group + ':log-stream:example-stream'
        for action in ['logs:CreateLogStream', 'logs:PutLogEvents']:
            with self.subTest(action=action):
                self.assertTrue(self.grants(action, stream))
                self.assertFalse(self.grants(action, self.group))

    def test_logging_permissions_do_not_extend_to_other_functions(self):
        other_group = self.group + '-other'
        self.assertFalse(self.grants('logs:CreateLogGroup', other_group))
        for action in ['logs:CreateLogStream', 'logs:PutLogEvents']:
            self.assertFalse(self.grants(action, other_group + ':log-stream:example-stream'))
