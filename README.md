# ARFSA Training Database

A Flask application that imports the two supplied Excel datasets into a single online database while preserving each source record separately.

## Included

- Separate AE and FH dashboards plus a cross-project interaction dashboard
- Gender, age group, CBF, date, and status filters
- Topic-level training, adoption, follow-up, waiting, and refresher-training metrics
- A separate progress page and branded PDF report for every CBF
- Individual add/edit/archive/restore workflows for both datasets
- Stable farmer IDs across manually confirmed cross-dataset matches
- Conservative identity suggestions with mandatory manual review
- Guided CBF field entry for centralized training attendance and follow-up scores
- Audit history for application changes
- A reserved bulk-upload screen for the later upload specification

The imported database currently contains 2,908 AE records and 1,541 FH records. All 407 suggested cross-dataset identities require manual review before they enter the Combined analysis.

## Run locally

The project-specific environment is `.venv-app`. From PowerShell:

```powershell
$env:ARFSA_USERNAME = "admin"
$env:ARFSA_PASSWORD = "choose-a-long-password"
$env:ARFSA_SECRET_KEY = "choose-a-long-random-secret"
.\run.ps1
```

Open `http://127.0.0.1:5000`. If environment variables are omitted, the development login is `admin` / `change-me-now`; do not use that default for deployment.

To recreate the environment on another computer:

```powershell
py -3.13 -m venv .venv-app
.\.venv-app\Scripts\python.exe -m pip install -r requirements.txt
```

To rebuild a new database from the original workbooks:

```powershell
.\.venv-app\Scripts\python.exe .\scripts\import_data.py --reset
```

`--reset` replaces application data, so it should not be run after users have started entering changes unless a replacement is intentional.

## Business rules

- The Excel adoption threshold is retained at 40.
- A score above 40 is counted as confirmed adoption.
- The follow-up interval defaults to the requested three months and can be changed with `ARFSA_FOLLOWUP_MONTHS`.
- Time-sensitive follow-up statuses are recalculated automatically once per day.
- Dropped-out people remain in received-training and adoption outputs but are excluded from current follow-up and training-needs counts.
- A low third-cycle score remains a refresher-training need. This corrects the workbook behavior that can show the third cycle as completed.
- Care-group attendance uses `A` as attended and `X` as not attended. A module is complete when all its sessions are attended.
- Numeric care-group ages are grouped as Youth (35 or younger), Adult (36-59), and Elder (60+); the original age is always retained.

## Identity matching

The two datasets remain separate. Confirmed matches share only a stable `ARF-######` farmer ID. Candidate generation uses an exact normalized name or phone with corroborating information, but never confirms a person automatically. Conflicting non-empty phone numbers are excluded. Every candidate appears under **Identity review** and enters the Combined dashboard only after a user presses **Confirm match**.

## Combined analysis

Combined is reserved for relationships between AE exposure/adoption and FH outcomes among manually confirmed farmers. It reports cross-project correlations, FH participation by AE exposure level, topic-level differences, and the underlying farmer observations. The current FH workbook contains attendance and module-completion data but no nutrition outcome score; nutrition-score analysis will become available once such a field is added to FH data.

## CBF data entry

The **Administration → Data entry** page lets a CBF select their name and record either a centralized training or a farmer follow-up. Centralized training lists CT-eligible farmers separately for each topic and records the event date, location and attendance. Follow-up entry lists only farmers with due follow-ups and accepts scores only for their FU topics. Every submission updates the AE record, recalculates topic status, creates an event-history entry and is written to the audit log.

## Tests

```powershell
.\.venv-app\Scripts\python.exe -m unittest discover -s tests -v
```

Deployment is intentionally left open until the hosting location is chosen. Credentials, the secret key, HTTPS, backups, and database hosting must be configured before exposing the application beyond the internal network.
