# Script inventory

Run Python commands from the repository root. Field-laptop configuration lives
in `config/instruments.json`; copy it from `config/instruments.example.json`.

## Field laptop

- `field/acquire_serial.py` continuously records NO2, NEPH and CO2 at 38400
  baud. It replaces PuTTY and writes the configured local acquisition files.
- `field/upload_to_aws.py` incrementally reads all five instrument sources and
  uploads raw bytes to S3 Bronze without applying scientific transformations.
- `field/windows/install_tasks.ps1` installs exactly two Windows tasks: the
  continuous serial logger and the repeating AWS uploader.
- `field/macos/install_upload_schedule.sh` and `field/macos/run_uploader.sh`
  provide the optional macOS upload schedule.

## AWS administration

- `aws/deploy_backend.py` deploys the API, Silver builder, data-quality Lambda,
  storage, schedules and related backend resources.
- `aws/deploy_upload_monitor.py` deploys the independent upload-freshness
  monitor and its notification schedule.
- `aws/provision_team_auth.py` provisions the private Team Console Cognito
  resources.
- `aws/manage_api_keys.py` administers researcher API keys.

## Quality checks

- `quality/audit_public_exposure.py` scans the repository and frontend bundle
  for accidental public exposure of secrets or private controls.
- `quality/check_public_api.py` performs read-only production API smoke tests.

The former root-level wrappers and split serial/upload config templates were
removed. The duplicate Team Console deployment script was also removed because
`aws/deploy_backend.py` already owns those resources.
