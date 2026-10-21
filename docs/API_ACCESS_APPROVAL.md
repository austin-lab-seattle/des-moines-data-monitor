# API access approvals

The requester submits name, email, organization and intended use, then confirms
email ownership with an explicit form submission. Link previews never confirm
ownership or issue a key. Verified requests expire seven days after submission.

With `ENABLE_EMAIL_LINK_APPROVAL=1`, Elena, Yeon and Pavan receive separate
notification emails with Approve and Reject links. Jeff's notifications remain
deferred. A link opens a compact request preview with an explicit confirmation
button. No active Team Console session, account login or MFA is required for
this email-link path. GET previews never consume a link or decide access.

**Security model:** possession of the email link authorizes its specific
decision on its specific request. Anyone receiving a forwarded link can use it;
it does not prove the clicker's identity. This tradeoff was explicitly accepted
by the project owner. Do not forward these emails. Audit records identify the
link's recipient with an `email-link:` subject and `identity_verified=false`,
not a Cognito/MFA-authenticated person. This authority cannot edit measurements,
list users, revoke keys or make decisions on other requests.

Links expire 24 hours after generation, or when the request expires if sooner.
The first conditional decision claim consumes **all** links for that request,
including those sent to other approvers. Each recipient and each decision has a
different token. Only token hashes and a nonce are stored; retrying notification
delivery derives the same links with a domain-separated server-secret HMAC,
without storing bearer tokens or invalidating links already received.

The MFA-protected Team Console remains a fallback. Its request-ID-only links
grant no authority and still require a verified, allowlisted team identity and
appropriate role. Older notification emails retain that fallback behavior;
deploying this change does not resend them or send any unsolicited emails.

## Readiness checks before a real request

A deployed page and passing offline tests are not a completed live email test.
Check the following without creating fake production requesters or issuing keys:

- The deployed API source matches the reviewed release, registration is enabled,
  and the notification/approver list contains only the intended recipients.
- Both private approval routes still require API Gateway JWT authentication; unsigned
  calls must return 401. Invalid public submissions must not create requests or
  send mail. Verification-link and email-decision GET requests never change state.
  Email decisions require an unexpired, single-use token plus explicit POST.
- For the Team Console fallback, each approver has an enabled, confirmed Cognito account, its intended email
  address has `email_verified=true`, and its group is permitted by the API.
  `CONFIRMED` alone does not prove that email verification is complete. SES
  sender verification is separate from the team-login email-verification flag.
- The team pool still requires MFA. Accounts without an enrolled authenticator
  must complete the normal sign-in/setup flow before using the console.

The API intentionally returns 403 for an approver whose email is not verified.
Do not remove this check, mark an account verified without verifying ownership,
or reset an account to make a smoke test pass. If the operator identity cannot
read group membership or request records, report that limitation and use the
normal authorized team sign-in; do not broaden operator permissions or bypass
API Gateway authentication.

The final end-to-end check needs a genuine consenting requester, delivery of the
verification email, explicit verification, receipt of an approver notification,
explicit email-link confirmation without a team session (or normal MFA-protected
console approval), and delivery of a read-only key only to that verified requester.
Obtain permission for notification emails before running that live test.

## Self-service team email verification

The Team Console shows an email-verification screen for signed-in accounts whose
ID token does not contain `email_verified=true`. The user explicitly requests a
code for their own account email and submits it to Cognito. Opening the page,
signing in, or viewing an email link never automatically sends a code. Failed or
expired codes never unlock the console. After verification, the user signs in
again for a fresh ID token; the frontend never flips the verification flag in
session storage or bypasses the API's verified-email and role checks. MFA remains
required and no password or account reset is performed.

This uses Cognito's standard own-account operations,
[GetUserAttributeVerificationCode](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_GetUserAttributeVerificationCode.html)
and [VerifyUserAttribute](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_VerifyUserAttribute.html).
Their required `aws.cognito.signin.user.admin` scope authorizes self-service user
pool APIs, not team-admin roles or AWS IAM access. Tokens/codes are sent only in
HTTPS POST bodies to the regional Cognito service, never in links or logs. The
UI does not accept an arbitrary destination email or mutate attributes itself.

Before publishing this frontend, enable that scope on the existing public client
with the targeted script, using an already-authorized operator identity:

```sh
python scripts/aws/enable_team_email_verification.py --credentials-file /secure/path/aws_creds.json
python scripts/aws/enable_team_email_verification.py --credentials-file /secure/path/aws_creds.json --apply
```

The first command is read-only. The second backs up the existing client, adds
only the allowed scope, preserves all other returned mutable client settings and
checks the readback. It never sends codes, marks users verified, changes roles,
weakens MFA, changes IAM permissions or deploys Lambdas. Cognito has no revision
guard for this operation; coordinate with other auth operators during the update.
Missing AWS authority is a blocker. Do not run the broad team-provisioning script
just to enable this scope. Existing sessions may need a new sign-in to obtain it.

## Authorization and delivery

`ApiApprover` is deliberately narrower than `Admin` or `AccessManager`. It does
not grant record edits, user management, revocation or general audit access.
For console decisions, the API checks both the authenticated role and the configured approver address
and requires a verified account email. Existing roles and active researcher
keys are preserved.

Approval records the actor and an audit event. Competing decisions on the same
request are conditional and atomic: only one can claim key delivery. Keys are
read-only, hashed at rest, never returned to an approver, and emailed only to the
verified requester. A pending-delivery key cannot authenticate. If delivery
fails, it is revoked and the request returns to the approval queue for explicit
console retry. Email links remain consumed after a delivery failure; never replay
them to mint another key. Token authority, decision, recipient, expiry and request
status are checked in the same conditional claim as key creation, not just during
the page preview. A process crash with an `APPROVING` request requires operator inspection;
do not issue a replacement without checking delivery and the recorded key state.

If an approver notification fails, the requester can retry their verification
link during its 30-minute lifetime. A short notification lease prevents normal
parallel retries from sending duplicate messages. After an ambiguous mail-send
timeout, a duplicate notification is possible, but duplicate decisions cannot
create a second key for that request. The private queue remains the source of
truth when email delivery is delayed.

## Email-link rollout

Use the narrowly scoped release script, not the original provisioning script:

```sh
python scripts/aws/deploy_email_approvals.py --credentials-file /secure/path/aws_creds.json
python scripts/aws/deploy_email_approvals.py --credentials-file /secure/path/aws_creds.json --apply
# Publish frontend/vercel.json and wait for the production deployment to succeed.
python scripts/aws/deploy_email_approvals.py --credentials-file /secure/path/aws_creds.json --apply --enable
```

The first command is read-only. Staging updates only API code and adds GET/POST
email-decision routes with token authorization inside the handler. Enabling first
checks the production rewrite with a harmless tokenless GET. It adds only the
feature flag to the preserved environment. The script guards the AWS account,
reviewed live-source baseline, approver list, JWT-protected private routes, MFA
and invite-only settings, and performs readbacks. It never changes accounts,
roles, IAM, instrument processing or Silver, and never sends emails or issues
keys. Protected rollback files contain secrets and must never be shared.

Rollback by setting only `ENABLE_EMAIL_LINK_APPROVAL=0` while preserving all other
Lambda environment values. This disables outstanding email links and restores
console-only notifications; existing issued keys and console access are unchanged.
Do not change the key-hash pepper to revoke links: that would break existing keys.
Tokens appear in email URLs; avoid recording query strings in application,
proxy or analytics logs. Pages use no external resources or scripts and send
`no-store`, `no-referrer`, restrictive CSP and frame-denial headers.

## Original Team Console provisioning

The original rollout below is historical bootstrap guidance, not the routine
email-link deployment path. It may invite missing accounts, so do not rerun it
merely to publish approval-link changes.

First run all tests with `python -m pip install -r requirements-test.txt` and
`python -m unittest discover -s tests`. The approval integration suite uses an
offline emulator with actual transaction/condition semantics. No CI test sends
real email, grants real access or creates monitoring records.

Run the targeted deployment with an already-authorized operator identity:

```sh
python scripts/aws/deploy_access_approval.py --credentials-file /secure/path/aws_creds.json
python scripts/aws/deploy_access_approval.py --credentials-file /secure/path/aws_creds.json --apply
```

The first command is a read-only preflight. The second requires committed,
reviewed source. It verifies the account, existing API baseline, MFA and invite-only
settings, and the JWT authorizer. It adds the dedicated approval role for the
three currently enabled team members (Elena, Yeon and Pavan), sends an invitation
only if an account is missing, and leaves Jeff's invitation and notifications
deferred. It adds two JWT-protected approval routes and a verification POST route,
and updates
only the dashboard API. Existing environment values and secrets are merged back.
It does not update Silver processing, instruments, aircraft routes or monitoring.
It never expands its own operator permissions. Missing AWS authority is a blocker,
not a reason to weaken private-route roles or skip email-token authorization.

The script saves the previous API package and configuration in an operator-only
temporary backup before deployment. The configuration contains secrets: never
commit it, email it or include it in diagnostic output. Verify code/environment
and route readbacks before publishing the frontend. Test real team sign-in and a
genuine request when the approvers are available; do not create fake production
requesters or bypass MFA just to claim an end-to-end success.
