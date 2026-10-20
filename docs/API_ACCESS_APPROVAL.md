# API access approvals

The requester submits name, email, organization and intended use, then confirms
email ownership with an explicit form submission. Link previews never confirm
ownership or issue a key. Verified requests expire seven days after submission.

The configured approvers receive a notification with a link to the private Team
Console. They sign in with their invited account and required authenticator-app
MFA, inspect the request and confirm approval or rejection. The email link
contains only a request identifier, not credentials or approval authority.
Forwarding that link does not grant the recipient permission to approve.

## Authorization and delivery

`ApiApprover` is deliberately narrower than `Admin` or `AccessManager`. It does
not grant record edits, user management, revocation or general audit access.
The API checks both the authenticated role and the configured approver address
and requires a verified account email. Existing roles and active researcher
keys are preserved.

Approval records the actor and an audit event. Competing decisions on the same
request are conditional and atomic: only one can claim key delivery. Keys are
read-only, hashed at rest, never returned to an approver, and emailed only to the
verified requester. A pending-delivery key cannot authenticate. If delivery
fails, it is revoked and the request returns to the approval queue for explicit
retry. A process crash with an `APPROVING` request requires operator inspection;
do not issue a replacement without checking delivery and the recorded key state.

If an approver notification fails, the requester can retry their verification
link during its 30-minute lifetime. A short notification lease prevents normal
parallel retries from sending duplicate messages. After an ambiguous mail-send
timeout, a duplicate notification is possible, but duplicate decisions cannot
create a second key for that request. The private queue remains the source of
truth when email delivery is delayed.

## Targeted rollout

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
four requested team members, sends an invitation only if an account is missing,
adds two JWT-protected approval routes and a verification POST route, and updates
only the dashboard API. Existing environment values and secrets are merged back.
It does not update Silver processing, instruments, aircraft routes or monitoring.
It never expands its own operator permissions. Missing AWS authority is a blocker,
not a reason to weaken the role or switch to unauthenticated email approvals.

The script saves the previous API package and configuration in an operator-only
temporary backup before deployment. The configuration contains secrets: never
commit it, email it or include it in diagnostic output. Verify code/environment
and route readbacks before publishing the frontend. Test real team sign-in and a
genuine request when the approvers are available; do not create fake production
requesters or bypass MFA just to claim an end-to-end success.
