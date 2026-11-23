# Release 1.5.0 — MIP Tracking Schema

**Service:** saathi-backend

> ⚠️ **Back up the database before deploying.** This release runs migrations
> `0105`–`0108`. They only add new tables and don't change any existing one.

---

## Table of Contents

- [What's New](#whats-new)
- [Data Model](#data-model)
- [Migrations](#migrations)
- [Dependencies](#dependencies)
- [Deployment](#deployment)
- [References](#references)

---

## What's New

- **MIP tracking tables**: `MipCheckInRule`, `Mip`, `MipDetails` and `MipStory`,
  each with a `simple_history` table.
- **MIP enums**: `MipStatus`, `MipStoryStatus`, `MipStoryProvider`,
  `MipCheckInTrigger` and `MipCheckInSchedule`.
- **Schema only**: no API, WebSocket, admin, Celery or bot-config changes.
  Nothing reads or writes the new tables yet.

---

## Data Model

<details markdown="1">
<summary>New tables</summary>

| Model | Table | Holds | Relations |
|---|---|---|---|
| `MipCheckInRule` | `chatbot_mipcheckinrule` | Check-in question config: trigger, schedule, message, chips | `flow` → `Flow` |
| `Mip` | `chatbot_mip` | One row per generated MIP: owner, chat, status, dates, next check-in | `profile`, `chat_session`, `next_check_in_rule` |
| `MipDetails` | `chatbot_mipdetails` | Plan content: goal, objective, duration, action steps, files | 1:1 `mip` |
| `MipStory` | `chatbot_mipstory` | Story report from an external provider (Mitra) | 1:1 `mip` |

Foreign keys to existing tables are `PROTECT`. Once MIP rows exist, the linked
`Flow`, `Profile` or `ChatSession` can't be deleted.

Source: [`chatbot/models/mip_models.py`](../../../../chatbot/models/mip_models.py)

</details>

<details markdown="1">
<summary>Constraints & indexes</summary>

| Table | Rule |
|---|---|
| `chatbot_mipcheckinrule` | Unique (`flow`, `code`). Only the parameter for `schedule_type` is set. |
| `chatbot_mip` | `STARTED` needs `start_date`. `end_date` is set only when status is `COMPLETED`. `next_check_in_rule` and `next_check_in_on` are both set or both empty. |
| `chatbot_mipdetails` | `duration_days > 0`. `files` has `pdf` and `docx`. |
| `chatbot_mipstory` | `GENERATED` needs `files.pdf` and `generated_at`. A session ID needs a provider. Unique (`provider`, `external_session_id`). |

Indexes on `chatbot_mip`: (`user_id`, `status`), plus a partial index on
`next_check_in_on` for rows with a check-in due.

</details>

<details markdown="1">
<summary>Enums</summary>

| Enum | Values |
|---|---|
| `MipStatus` | `GENERATED`, `STARTED`, `PAUSED`, `COMPLETED`, `CLOSED` |
| `MipStoryStatus` | `DECLINED`, `IN_PROGRESS`, `GENERATED` |
| `MipStoryProvider` | `MITRA` |
| `MipCheckInTrigger` | `MIP_CREATED`, `START_DATE_SET`, `TIMELINE_ENDED`, `COMPLETION_CLAIMED`, `MIP_COMPLETED`, `STORY_LEFT` |
| `MipCheckInSchedule` | `DAY_OFFSETS`, `EVERY_N_DAYS`, `PERCENT_OF_TIMELINE` |

Source: [`chatbot/models/enums.py`](../../../../chatbot/models/enums.py)

</details>

---

## Migrations

<details markdown="1">
<summary>0105–0108 (after <code>0104_media_template</code>)</summary>

| Migration | Creates |
|---|---|
| `0105_mip_check_in_rule` | `chatbot_mipcheckinrule` + history |
| `0106_mip` | `chatbot_mip` + history |
| `0107_mip_details` | `chatbot_mipdetails` + history |
| `0108_mip_story` | `chatbot_mipstory` + history |

All four are `CreateModel` only. None of them migrates data or alters an existing
table. Source: [`chatbot/migrations/`](../../../../chatbot/migrations/)

</details>

---

## Dependencies

- **Python packages**: none added (`pyproject.toml` and `uv.lock` unchanged).
- **Django**: `>=5.1` is required for `CheckConstraint(condition=...)`. `uv.lock`
  pins 5.2.
- **PostgreSQL**: required for the JSON key constraints (`has_key` / `has_keys`).
- **Env vars, Vault secrets, services, systemd units**: unchanged.

---

## Deployment

### Pre-deploy checklist

1.  **Confirm the database is at `0104`** (run on the backend host):

    ```bash
    cd /opt/deployment/backend/saathi-service/saathi-backend-service
    uv run --frozen python manage.py showmigrations chatbot | tail -1
    # [X] 0104_media_template
    ```

2.  **Back up the database.**

    <details markdown="1">
    <summary>pg_dump using the current <code>.env</code> (needs <code>postgresql-client</code>)</summary>

    ```bash
    cd /opt/deployment/backend/saathi-service/saathi-backend-service
    set -a; source <(grep -E '^(DATABASE_|PG_SSL_MODE)' .env); set +a
    PGPASSWORD="$DATABASE_PASSWORD" PGSSLMODE="$PG_SSL_MODE" pg_dump -Fc \
      -h "$DATABASE_HOST" -p "$DATABASE_PORT" -U "$DATABASE_USER" "$DATABASE_NAME" \
      > ~/saathi_pre_1.5.0_$(date +%Y%m%d%H%M).dump
    ```

    </details>

### Deploy steps

1.  **Deploy the `release-1.5.0_RC<n>` tag** using one of the two options below.

    **Option A: Jenkins (standard).** Run the backend deploy job with `repo_ref`
    set to the tag. The job runs [`deploy/ansible/deploy.yml`](../../../../deploy/ansible/deploy.yml),
    which applies the migrations, restarts `saathi-uvicorn` and `saathi-celery`,
    and waits for `/health/` to return 200.

    <details markdown="1">
    <summary>What the playbook does</summary>

    1. Clones `repo_ref` into `/opt/deployment/backend/saathi-service/releases/<timestamp>`.
    2. Fetches `config/secrets.json` (Vault `saathi_gcp_credentials`) and runs `uv sync --frozen`.
    3. Replaces the live directory `.../saathi-backend-service` with the release.
    4. Writes `.env` from Vault `saathi_backend_env` via [`scripts/json2env.sh`](../../../../scripts/json2env.sh).
    5. Runs `manage.py migrate --noinput`.
    6. Renders the systemd units from [`deploy/ansible/templates/`](../../../../deploy/ansible/templates/), then restarts both services.
    7. Polls `http://localhost:9000/health/` (20 tries, 5 s apart).

    There is no automatic rollback. Ports, worker counts and Vault paths are the
    `vars:` in `deploy.yml`, and Jenkins can override them.

    </details>

    **Option B: Manual.** Use this when Jenkins is unavailable. Run the same steps
    as the playbook by hand on the backend host, as root.

    <details markdown="1">
    <summary>Manual deployment steps (needs <code>git</code>, <code>uv</code>, <code>jq</code>, <code>curl</code> and the Vault token)</summary>

    ```bash
    sudo -i

    PROJECT=/opt/deployment/backend/saathi-service
    LIVE=$PROJECT/saathi-backend-service
    REL=$PROJECT/releases/$(date +%Y%m%d%H%M%S)
    REPO_URL=<repo_url>              # same value Jenkins passes
    REPO_REF=release-1.5.0_RC<n>
    VAULT=<vault_address>            # deploy.yml vars.vault_address (ends with /)
    TOKEN=$(cat /opt/deployment/.token)

    # 1. Fetch the release, secrets and .env (live site untouched)
    git clone --branch "$REPO_REF" "$REPO_URL" "$REL"
    mkdir -p "$REL/config"
    curl -sk "${VAULT}saathi_gcp_credentials" -H "X-Vault-Token: $TOKEN" \
      | jq '.data.data' > "$REL/config/secrets.json"
    curl -sk "${VAULT}saathi_backend_env" -H "X-Vault-Token: $TOKEN" \
      | jq '.data.data' | bash "$REL/scripts/json2env.sh" > "$REL/.env"

    # 2. Stop services and swap in the release
    #    (deletes the old logs/, as the playbook does; copy them first if needed)
    systemctl stop saathi-uvicorn saathi-celery
    rm -rf "$LIVE" && mv "$REL" "$LIVE"
    cd "$LIVE" && mkdir -p logs

    # 3. Install dependencies and apply migrations 0105–0108
    uv sync --frozen
    uv run --frozen python manage.py migrate --noinput

    # 4. Start services
    systemctl start saathi-uvicorn saathi-celery
    ```

    The systemd units are unchanged in 1.5.0. On a new host, render both
    templates from [`deploy/ansible/templates/`](../../../../deploy/ansible/templates/)
    with the `deploy.yml` vars. Write them to `/etc/systemd/system/`, then run
    `systemctl daemon-reload` and `systemctl enable saathi-uvicorn saathi-celery`.

    </details>

2.  **Verify.** The migrations should show as applied, both services should be
    active, and `/health/` should return 200:

    ```bash
    cd /opt/deployment/backend/saathi-service/saathi-backend-service
    uv run --frozen python manage.py showmigrations chatbot | tail -4   # 0105–0108 all [X]
    systemctl is-active saathi-uvicorn saathi-celery
    curl -fsS http://localhost:9000/health/
    ```

### Rollback

Redeploy `release-1.4.0_RC1` with Jenkins or the manual steps. The 1.4.0 code
doesn't use the new tables, so they can stay in place.

To drop the tables as well, run
`uv run --frozen python manage.py migrate chatbot 0104_media_template` **before**
redeploying, while the 1.5.0 code is still live, because the 1.4.0 checkout
doesn't include `0105`–`0108`. This deletes all MIP rows.

---

## References

- Deployment: [`deploy.yml`](../../../../deploy/ansible/deploy.yml),
  [systemd templates](../../../../deploy/ansible/templates/),
  [`json2env.sh`](../../../../scripts/json2env.sh)
- Code: [`mip_models.py`](../../../../chatbot/models/mip_models.py),
  [`enums.py`](../../../../chatbot/models/enums.py),
  [migrations](../../../../chatbot/migrations/)
- Docs: [Models](../models.md), [Enums](../../../backend/enums.md),
  [Developer Setup](../../../setup/developer_setup.md)
