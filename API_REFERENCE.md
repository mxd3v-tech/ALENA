# API Reference (`/api/v1`)

A small REST layer for integrating **АЛЁНА** (Автоматический Ловец Ёрничества,
Небрежности и Аутентичности) – the report checker – into other products.
API errors return JSON. Successful requests return JSON except for HTML reports and PDF exports.

## Authentication

Two interchangeable methods are accepted:

- **API key (machine-to-machine):** send the header `X-API-Key: <key>`. Keys
  belong to an account and are issued in **Пользователи** with the *Выдать ключ
API* action.
  The account must have the permission «Пользоваться API по ключу» (`use_api`);
  without it the key is rejected. `AU_API_KEY` from the environment is assigned
  to the first administrator when the instance bootstraps.
- **Browser session:** a logged-in session cookie also authorizes the API, which
  is what the built-in UI uses. Session-authorized calls that change state
  (`POST`, `DELETE`) must also carry the CSRF token of that session in the
  `X-CSRF-Token` header – the UI reads it from `<meta name="csrf-token">`.
  Without it the call is refused:

  ```json
  { "error": "Сессия устарела – обновите страницу и повторите действие." }
  ```

  Calls authorized by `X-API-Key` are exempt: a browser never attaches that
  header to a cross-site request, so there is nothing to forge.

If both a session cookie and `X-API-Key` are present, the explicit API key wins;
the request never falls back to the session when that key is invalid.

On a missing, wrong or unauthorized key the API responds `401`:

```json
{ "error": "Не авторизовано: передайте заголовок X-API-Key или войдите в сессию." }
```

Errors use a consistent shape: `{"error": "<message>"}`, with `status` added for
HTTP errors raised by the server (e.g. `404`).

### Roles and visibility

Every call acts **on behalf of the account behind the key or session**, and the
data is scoped to it:

| Role | Sees |
|------|------|
| `teacher` | Only own checks. Fingerprint base: own entries, plus those of colleagues in the same teacher group. Borrowing is searched inside that base only. |
| `admin` | Everything, when the account keeps the `see_all` permission. |

Teacher groups (managed by an admin in the web UI, `/admin/teams`) widen the
fingerprint base only – never the job history. An account in no group behaves
exactly as before: its base is its own.

Reading another teacher's check requires `see_all`. Changing it is a separate
decision: a teacher may never change another teacher's data, regardless of
`see_all`; an administrator additionally needs `delete_all` to delete it.
Otherwise the call answers `403`:

```json
{ "error": "Эта проверка принадлежит другому преподавателю.", "status": 403 }
```

Actions also honour per-account permissions: `run_checks` for `POST /jobs`,
`delete_own` for the caller's data, `delete_all` plus the `admin` role for other
teachers' data, and `manage_base` for `DELETE /memory/<key>`.
Reading a check the account already owns (`GET /jobs`, `/jobs/<id>`, its report
and export) needs no extra permission beyond authentication – revoking
`run_checks` stops new checks, it does not hide the account's own history.
Stopping a check (`POST /jobs/<id>/cancel`) likewise needs only ownership: the
account that started it may call it off without the right to delete it.

## Base URL

```
http://<host>:5000/api/v1
```

## Endpoints

### GET `/health`
Public liveness check. No auth. `db` reflects whether `DATABASE_URL` is set
(Postgres) or the JSON fallback is in use.

```json
{ "ok": true, "weasyprint": true, "db": true }
```

### POST `/jobs`
Start a check. `multipart/form-data`:

| Field | Type | Notes |
|-------|------|-------|
| `files` | file(s) | One or more `.pdf`, `.docx`, `.odt` or `.doc` files, or a `.zip` of them. Repeat the field for several files. DOCX, ODT and DOC are converted to PDF on the server before checking, so the criteria apply to every format identically; a file that cannot be converted is reported as a card with an error and the rest of the batch is checked as usual. The combined file size must fit `AU_MAX_UPLOAD_MB` (default 5120 MiB). The complete multipart request also has a 600 MiB limit. The web interface can upload larger batches in chunks within the configured batch limit. |
| `threshold` | float | Text-similarity threshold, `0.0`-`1.0` (default `0.6`). Values outside the range are clamped to it; anything unparsable (`abc`, `NaN`, `inf`, empty) falls back to the default instead of failing the request. |
| `gost` | string | Optional comma-separated GOST check codes to evaluate, e.g. `S1,S3,F7,F9`. Omit the field to run all 22 checks; an empty value runs none. Codes: `S1`-`S9` plus `S5A` (structural elements), `F1`-`F12` (formatting). |
| `use_memory` | string | Optional. `0`/`false` skips plagiarism comparison against the stored fingerprint base (only files within the batch are compared). New reports are still added to the base. Default `1`. |
| `weights` | string | Optional per-criterion weight for the recommended grade, `0`-`100` each: `S1:100,F1:20,F2:100`. Codes not listed keep `100`. The weights of the criteria actually selected are normalised to sum to 100. Omit the field to use the weights set by the administrator. |
| `scale` | int | Optional grade scale, `2`-`100`. Omitting it uses the grade scale saved by the administrator (initially `100`). A scale of `100` shows percentages. Other scales also show the grade in points. |

`201 Created`:

```json
{
  "job_id": "a1b2c",
  "status": "processing",
  "links": {
    "self":   "/api/v1/jobs/a1b2c",
    "report": "/api/v1/jobs/a1b2c/report",
    "export": "/api/v1/jobs/a1b2c/export"
  }
}
```

New jobs use five lowercase hexadecimal characters for `job_id`. Use the full
ID returned by the API for older jobs too, since their IDs can be longer.

`400` when no files are supplied or no report is found inside the upload.
`413` when the files exceed the configured batch limit or the multipart body exceeds the request limit.

### GET `/jobs`
List the jobs visible to the caller, keyed by `job_id`:

```json
{
  "a1b2c": {
    "status": "done",
    "progress": 100,
    "step": "Готово! Проверено 12 отчётов, сохранено в базу: 12.",
    "total": 12,
    "done_files": 12,
    "text_pairs": 3,
    "img_pairs": 1,
    "created_at": "28.06.2026 11:40",
    "beat": 1782639600.42,
    "owner": "sokolova",
    "owner_fio": "Соколова Елена Викторовна",
    "threshold": 60,
    "summary": {
      "group": "ПР-21-1",
      "groups": ["ПР-21-1"],
      "gost": 87,
      "plag": 63,
      "plag_flagged": true,
      "threshold": 60,
      "clean": 8,
      "grade": 74,
      "grade_score": 7.4,
      "scale": 10,
      "weighted": true,
      "no_text": 1,
      "students": [
        { "work_id": 1, "fio": "Петров П. П.", "group": "ПР-21-1", "gost": 61,
          "plag": 63, "plag_flagged": true, "no_text": false, "fails": ["S2", "F3", "F7"],
          "flaws": [
            { "code": "S2", "text": "Отсутствует лист задания на практику (курсовую работу)",
              "details": "Лист задания не обнаружен" }
          ],
          "grade": {
            "pct": 58, "score": 5.8, "scale": 10, "criteria": 21,
            "lost": [{ "code": "F3", "name": "Основной текст 14 пт", "weight": 9.9 }]
          },
          "error": null }
      ],
      "matches": [
        { "a_id": 1, "b_id": 13, "a": "Петров П. П.", "b": "Белов А. Р. · ПР-20-1", "pct": 63,
          "kind": "текст", "where": "база, 18.06.2025" }
      ],
      "matches_total": 7,
      "fail_counts": [["F7", 4], ["S2", 2]]
    },
    "error": null
  }
}
```

`status` is one of `processing`, `done`, `error` and `cancelled` – the last one
means the check was stopped on request (see `POST /jobs/<id>/cancel`) and has no
report and no summary. `summary` is `null` until the check finishes;
`matches[].pct` is `null` for duplicate images, which are a yes/no match rather
than a share of the text.

`summary.groups` lists all identified groups in the uploaded batch. The older
`group` field contains the first group in sorted order, or `–` for an ungrouped
batch. File IDs are local to one job. Each uploaded file has a `students[].work_id` and its
original `filename`. The fields `a_id` and `b_id` identify the two files in a match. Archive versions also
receive distinct IDs. For image matches, `pages` contains page numbers in file A
and `b_pages` contains page numbers in file B. Older summaries can omit the IDs
and the page numbers for file B. For older jobs the server reconstructs the group
list from the available student records.

`students[].plag` shows the highest text similarity across the batch and the
visible archive when archive comparison is enabled. Percentages can be
fractional. Use `students[].plag_flagged` for the feedback decision. It is true
when at least one eligible text comparison reached the configured threshold
before rounding. `summary.plag_flagged` reports the same decision for the whole
batch. Older summaries can omit these fields.

`students[].gost` is `null` when no GOST criterion was checked for that work.
`summary.gost` averages the scored works, including zero scores, and is `null`
when none were scored. `summary.clean` counts works that passed every checked
criterion and had at least one criterion checked.

`students[].no_text` is `true` when too little text could be extracted from the
file to compare it with anything – a scan, or a PDF whose fonts carry no usable
encoding. Such a work takes no part in the similarity matrix at all and its
`plag` is `null`, not `0`: borrowing in it is *unknown*, not *absent*, and has
to be checked by hand. Its `grade` is also `null`: a formatting mark is not
reported for a document whose text could not be read. `summary.no_text` counts
such works for the batch.
`matches` carries at most the 500 most prominent matches and `matches_total`
says how many there were: a course of screenshot-heavy reports produces tens of
thousands, and the digest is re-sent on every poll of the check list. The full
picture is in the HTML report, which likewise shows the closest image pairs
rather than all of them.

`beat` is a Unix timestamp the running check refreshes every 15 seconds. A
check still marked `processing` whose `beat` is over 90 seconds old is treated
as lost – the process that ran it died – and is reported as an error on the
next read. Clients need not act on it; poll `status` as before.

`grade` is the recommended formatting mark in percent – the share of the
normalised criterion weights that the work earned; `grade_score` restates it in
points of `scale` and is `null` when the scale is percent. `weighted` is `true`
when the weights were not all equal. `students[].flaws` is the flat list the
teacher pastes to the student portal: one plain-language line per failed
criterion, in GOST order, with the checker's own wording in `details`.
Checks that ran before this feature existed have no `grade` or `flaws`.

### GET `/jobs/<job_id>`
Poll one job. Same shape as a list entry. The record is read from the running
check when there is one, otherwise from stored history – so a restart does not
lose it. `404` when the id is unknown or malformed.

### POST `/jobs/<job_id>/cancel`
Request cancellation of a running check, including during HTML generation. A
successful response means the worker will discard the report and fingerprints,
remove the uploaded files and finish with `status: "cancelled"`. Publication and
cancellation are serialized. If publication finishes first, the server returns
`409` and the saved result is available.

```json
{ "ok": true }
```

### GET `/jobs/<job_id>/report`
The self-contained HTML report (`Content-Type: text/html`). `404` if missing.

### GET `/jobs/<job_id>/export`
The report as PDF (`Content-Type: application/pdf`, attachment). `404` if the
report is missing, `501` if WeasyPrint is not installed on the server.

### DELETE `/jobs/<job_id>`
Delete a single job: its history entry (memory + DB) and the saved HTML report.
Stored student fingerprints are kept – remove them via `DELETE /memory/<key>`.
The owner needs `delete_own`; deleting another teacher's visible job requires
both the `admin` role and `delete_all`. `409` while the job is still processing,
`404` if nothing was found.

```json
{ "ok": true }
```

### DELETE `/jobs?scope=own|all`
The `scope` query parameter is mandatory. Omitting it or using any other value
returns `400` without deleting anything.

- `scope=own` deletes only the caller's jobs, report files and fingerprints and
  requires `delete_own`. `see_all` never widens this scope; colleagues' entries
  visible through a teacher group are not touched.
- `scope=all` deletes all jobs, report files and fingerprints. It requires the
  `admin` role, the separate `delete_all` permission, and `confirm_login` equal
  to the current administrator's login.

Either scope returns `409` without deleting anything while a targeted check is
still processing. Stop it or wait for it to finish first.

```bash
curl -X DELETE -H "X-API-Key: $KEY" "$BASE/jobs?scope=own"
curl -X DELETE -H "X-API-Key: $KEY" \
  "$BASE/jobs?scope=all&confirm_login=admin"
```

```json
{ "ok": true, "scope": "own", "cleared": 4, "cleared_store": 21 }
```

### GET `/memory`
List stored student fingerprints (no text or hashes, just metadata). A teacher
sees their own base plus the bases of colleagues in the same teacher group;
check the `owner` field to tell them apart:

```json
[
  {
    "key": "sokolova|иванов иван|пр-21-1|v1",
    "version": 1,
    "filename": "Иванов_отчёт.pdf",
    "student": { "name": "Иванов Иван", "group": "ПР-21-1" },
    "pages_count": 14,
    "image_count": 3,
    "added_at": "20.06.2026 09:35",
    "job_id": "a1b2c",
    "owner": "sokolova"
  }
]
```

The key is `<owner>|<name>|<group>|v<N>` – the owner prefix is what keeps two
teachers' bases apart when they have a namesake in the same group.

### DELETE `/memory/<key>`
Delete one fingerprint entry. Requires the `manage_base` permission. The `key`
is the value from `GET /memory` and must be URL-encoded (it contains `|` and
spaces). Another teacher's entry additionally requires the `admin` role and
`delete_all`; otherwise the result is `403`. A missing key returns `404`.

Being in the same group grants no delete rights: a colleague's entry is visible
in `GET /memory`, but a teacher's `DELETE` on it still answers `403`.

```
DELETE /api/v1/memory/sokolova%7C%D0%B8%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%20%D0%B8%D0%B2%D0%B0%D0%BD%7C%D0%BF%D1%80-21-1%7Cv1
```

```json
{ "ok": true }
```

## Example: end-to-end with curl

```bash
KEY=your-api-key
BASE=http://localhost:5000/api/v1

# 1. Start a job
job=$(curl -s -X POST "$BASE/jobs" \
  -H "X-API-Key: $KEY" \
  -F "files=@reports.zip" -F "threshold=0.6" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# 2. Poll until done
while :; do
  st=$(curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job")
  echo "$st"
  echo "$st" | grep -q '"status": *"done"' && break
  echo "$st" | grep -qE '"status": *"(error|cancelled)"' && break
  sleep 2
done

# 2a. Or stop it early
# curl -s -X POST -H "X-API-Key: $KEY" "$BASE/jobs/$job/cancel"

# 3. Fetch outputs
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/report" -o report.html
curl -s -H "X-API-Key: $KEY" "$BASE/jobs/$job/export" -o report.pdf
```

## CORS

Not enabled by default (the UI is same-origin). To call the API from a browser
on another origin, add `flask-cors` and restrict it to trusted origins.
