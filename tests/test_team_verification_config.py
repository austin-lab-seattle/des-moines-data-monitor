import unittest
from scripts.aws.enable_team_email_verification import CLIENT, POOL, SELF_SCOPE, scope_update


class TeamVerificationConfigTests(unittest.TestCase):
    def current(self):
        return {"UserPoolId": POOL, "ClientId": CLIENT, "ClientName": "Existing team app",
            "AllowedOAuthFlows": ["code"], "AllowedOAuthFlowsUserPoolClient": True,
            "AllowedOAuthScopes": ["openid", "email", "profile"],
            "CallbackURLs": ["https://dashboard.example.org/team"], "LogoutURLs": ["https://dashboard.example.org/team"],
            "EnableTokenRevocation": True, "ReadAttributes": ["email", "email_verified"],
            "WriteAttributes": ["name"], "AccessTokenValidity": 60, "CreationDate": "readonly", "LastModifiedDate": "readonly"}

    def test_only_scope_changes_and_other_mutable_fields_are_preserved(self):
        current = self.current()
        fields = set(current) - {"CreationDate", "LastModifiedDate"}
        result = scope_update(current, fields)
        self.assertEqual(["openid", "email", "profile", SELF_SCOPE], result.pop("AllowedOAuthScopes"))
        self.assertEqual({k: v for k, v in current.items() if k in fields and k != "AllowedOAuthScopes"}, result)
        self.assertEqual(["openid", "email", "profile"], current["AllowedOAuthScopes"])

    def test_scope_is_not_duplicated_on_repeat(self):
        current = self.current()
        current["AllowedOAuthScopes"].append(SELF_SCOPE)
        self.assertEqual(1, scope_update(current, set(current))["AllowedOAuthScopes"].count(SELF_SCOPE))

    def test_foreign_secret_implicit_or_unrecognized_configuration_is_refused(self):
        fields = set(self.current())
        for change in ({"UserPoolId": "other"}, {"ClientId": "other"}, {"ClientSecret": "must-not-use"},
                       {"AllowedOAuthFlows": ["implicit"]}, {"AllowedOAuthFlowsUserPoolClient": False}, {"FutureSecuritySetting": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                scope_update({**self.current(), **change}, fields)


if __name__ == "__main__":
    unittest.main()
