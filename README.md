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

### Reviewing duplicate names

Open **Identity review → Duplicate names → AE** (or **FH**) and expand a name. Each record starts in its own person group, with the strongest suggested matches highlighted. Drag a record card onto another person group to combine them in your draft, or use the person selector. Double-click a record in a multi-record group to move it back into its own person group. Use **New person group** or **New person / individual** to split records back out. A name can be divided into any number of groups, including individuals.

The selected group's side-by-side comparison updates immediately. Profile conflicts are highlighted in red, missing values in yellow, and training-history differences in blue. Choose the values to keep for conflicting profile fields and for conflicting values attached to the same training event. Distinct training events are combined; groups exceeding the existing three-cycle limit must be reviewed separately. **Review split → Save identity split** applies the whole decision together, archives merged copies, and remembers which groups are different people. An administrator can reverse the complete decision through the audit log. Drafts stay on the current page until saved, and leaving with unsaved changes displays a browser warning.

Run the identity regression checks with `.venv-app\Scripts\python.exe -m unittest tests.test_identity_review`. Restart the Flask service and refresh the page after deploying the Python, template, JavaScript, and CSS files together.

## Combined analysis

Combined is reserved for relationships between AE exposure/adoption and FH outcomes among manually confirmed farmers. It reports cross-project correlations, FH participation by AE exposure level, topic-level differences, and the underlying farmer observations. The current FH workbook contains attendance and module-completion data but no nutrition outcome score; nutrition-score analysis will become available once such a field is added to FH data.

## CBF data entry

The **Administration → Data entry** page lets a CBF select their name and record either a centralized training or a farmer follow-up. Centralized training lists CT-eligible farmers separately for each topic and records the event date, location and attendance. Follow-up entry lists only farmers with due follow-ups and accepts scores only for their FU topics. Every submission updates the AE record, recalculates topic status, creates an event-history entry and is written to the audit log.

## AE Field App

The **Administration → AE Field App** is an installable, tablet-oriented PWA for offline AE data entry. It stores a CBF's prepared beneficiary worklist and questionnaires in IndexedDB, saves CT and follow-up submissions to a local outbox without requiring a connection, and uploads them when the user chooses **Synchronize now** or the app detects that internet has returned.

To prepare a CBF tablet:

1. In **User accounts**, create an **AE field user** and assign the account to one CBF.
2. Sign in on the tablet while online and open **AE Field App**.
3. Press **Prepare or update field data** and confirm the beneficiary count.
4. Install the app from the browser's **Add to Home screen** or **Install app** action if desired.
5. After fieldwork, open the app online and press **Synchronize now**. Keep every pending entry until it is confirmed as synchronized.

Each tablet submission has a unique client ID. Repeating an interrupted upload therefore cannot create the same event twice. Rejected submissions remain in the tablet's Pending list with the server message and can be retried or deliberately discarded. Do not clear the browser's site data while unsynchronized entries remain.

To update the installed app, connect to the internet, open **AE Field App**, and use **Check for updates** in the **App updates** panel. A small **New version available** notice appears directly beside that button when an update is found. When an update is ready, save any unfinished entry and press **Update and restart**. The app also checks on opening, returning to it, and reconnecting. Updates preserve prepared worklists and all saved submissions, including entries still waiting to upload. **Prepare or update field data** refreshes beneficiaries and questionnaires separately.

Uninstalling and reinstalling is not needed for normal updates. Avoid uninstalling or clearing site data with unsynchronized entries. For an older installation that does not yet show the update controls, connect, close every Field App window/browser tab, and reopen it; if needed, close and reopen once more after the updated app has downloaded.

Deploy app files together and restart the Flask service after a release. The offline release fingerprint is calculated from the app shell at startup, so changes are detected without manually incrementing a cache number. The new shell downloads completely before activation; failed downloads leave the existing offline app available. To run update regression checks, use `node --test tests/field-updates.test.cjs` and `.venv-app\Scripts\python.exe -m unittest tests.test_field_updates`.

When recording a follow-up, the user can optionally press **Add current location**. The browser asks for permission and stores the latitude, longitude, estimated accuracy and capture time with that offline submission. A follow-up can still be saved if location permission is declined or GPS is unavailable.

Follow-up questionnaires end with one optional photo of the best practice found during the visit, with a maximum of one photo. Selecting another photo replaces the first. Section photo prompts and the I1 training-ranking question have been removed, including from previously prepared tablet questionnaires after updating the app.

Saving a follow-up immediately opens its **Results**, including package and training scores, household outcome, RVO/Project checks, Breadth, Depth, findings to share with the beneficiary, and recommended next actions. This works without internet. The visit and its outcome are saved together on the tablet; use the **Results** tab or **View results** from Pending to reopen them. Results remain available after closing the app and after successful synchronization. Existing pending adaptive follow-ups receive local results when the updated app opens.

Selecting a farmer opens a focused follow-up screen. The other Field App actions and navigation remain hidden while you complete the questions, save the visit and review the results. **Finish and return to Field App** returns to the main screen only after the visit has been saved. **Cancel visit** asks before discarding an unfinished visit, and closing or reloading the app warns about unsaved answers.

Each saved follow-up has a persistent receipt in **Results** with two separate steps: **Saved on this tablet** and **Confirmed by server**. The receipt shows the saved time, the training types and any attached photos or location. The main Field App screen lists the selected CBF's pending visits by name, explicitly marked as saved on the device and still waiting for the server. Back at the office, connect and open the app to upload them. Keep the app open during synchronization and check that the server has confirmed the visit. Failed uploads keep their message and saved entries available for retry, even after reopening the app. Completing the questions alone does not save the visit; a failed tablet save leaves the form open with a **Not saved** message. The online results page also confirms that the visit is saved before the separate CBF decision step.

Tablet results are calculated using the bundled scoring rules. During synchronization the server recalculates the answers and returns the confirmed outcome, including on retries, which replaces the tablet calculation. Client-supplied scores are never accepted as authoritative. Once synchronized, **Review and confirm CBF decisions** opens the existing online decision form. The results history follows the tablet's selected CBF and live/testing environment.

The scoring rules and result wording are cached with the app shell. Deploy and restart the server, then use **Check for updates → Update and restart** on the tablet to enable offline outcomes. Run `.venv-app\Scripts\python.exe -m unittest tests.test_offline_scoring tests.test_offline_results tests.test_followup_survey tests.test_field_updates` to check scoring parity, sync responses, and offline release contents. The parity test compares JavaScript with Python across all question choices, score boundaries and more than 1,000 combined/conditional cases; it requires Node.js on PATH.

The focused visit and save-status browser checks run with `node --test tests/field-save-status.test.cjs`. They require Playwright, a browser, and Python with the application dependencies; use `ARFSA_TEST_PYTHON` to select Python and `ARFSA_TEST_BROWSER=msedge` to use installed Edge. These checks use a disposable database copy, isolated browser storage and simulated upload replies, including real offline reloads.

## Testing environment

Use **Open testing environment** on either **Data entry** or **AE Field App** to work in a personal copy of the live database. You can add practice CTs and follow-ups, including offline submissions, without changing live records. The yellow testing banner confirms that the mode is active. Choose **Exit testing** to return to live data. **Reset test data** discards all of your practice changes and makes a fresh copy of the current live database.

Service workers require HTTPS outside localhost. Production hosting must therefore use HTTPS, secure cookies, reliable backups and personal CBF accounts before field deployment.

## Tests

```powershell
.\.venv-app\Scripts\python.exe -m unittest discover -s tests -v
```

Deployment is intentionally left open until the hosting location is chosen. Credentials, the secret key, HTTPS, backups, and database hosting must be configured before exposing the application beyond the internal network.
