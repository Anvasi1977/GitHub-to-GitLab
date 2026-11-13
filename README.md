![GitHub to GitLab Sync](banner-v3.png)
# GitHub to GitLab Migration and Synchronization Tool

A Python utility for migrating repositories from GitHub to GitLab and configuring ongoing one-way synchronization from GitHub to GitLab.

The script uses the GitHub REST API and GitLab REST API to:

- discover repositories in a GitHub account or organization;
- create corresponding private projects in GitLab;
- import repository contents from GitHub into GitLab;
- configure GitLab pull mirroring when available;
- configure GitHub Actions-based synchronization as a fallback or additional sync mechanism;
- track successfully processed repositories to avoid repeated processing.

---

## Table of Contents

- [Overview](#overview)
- [Main Features](#main-features)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Installation](#installation)
- [Usage](#usage)
- [Generated Files](#generated-files)
- [Success Log](#success-log)
- [GitHub Actions Synchronization](#github-actions-synchronization)
- [Required Token Permissions](#required-token-permissions)
- [Limitations](#limitations)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Recommended `.gitignore`](#recommended-gitignore)
- [License](#license)

---

## Overview

This project automates repository migration from GitHub to GitLab.

It is designed for scenarios where a user has multiple repositories on GitHub and wants to create corresponding projects in GitLab, copy repository contents, and keep GitLab synchronized with future changes from GitHub.

The script supports two synchronization approaches:

1. **GitLab Pull Mirror**

   GitLab periodically pulls changes from GitHub.

   This depends on the GitLab plan, instance configuration, and available permissions.

2. **GitHub Actions Push Sync**

   GitHub Actions pushes changes from GitHub to GitLab.

   This is useful when GitLab pull mirroring is unavailable, not enabled, or not supported by the GitLab instance.

---

## Main Features

### Repository Discovery

The script retrieves GitHub repositories available to the configured GitHub token and filters them by the configured GitHub user or organization.

### GitLab Project Creation

For each GitHub repository, the script creates a corresponding private GitLab project under the configured GitLab namespace.

Repository names are sanitized to produce valid GitLab project paths.

### Import from GitHub

When creating a GitLab project, the script provides an authenticated GitHub import URL so GitLab can import repository contents.

### Existing Project Handling

If a GitLab project already exists:

- if the project is empty, the script can delete and recreate it;
- if the project is not empty, the script keeps it and continues processing.

This helps avoid accidental data loss.

### GitLab Pull Mirror Configuration

The script attempts to configure GitLab pull mirroring from GitHub to GitLab.

After requesting mirror configuration, it verifies the actual mirror status because a successful HTTP response does not always mean mirroring was actually enabled.

### Manual Pull Mirror Trigger

If pull mirroring is enabled, the script attempts to manually trigger the mirror process.

### GitHub Actions Synchronization

The script can create or update a GitHub Actions workflow in each GitHub repository.

The workflow pushes branches and tags from GitHub to GitLab whenever changes occur.

### Protected Branch Handling

The generated GitHub Actions workflow can temporarily unprotect GitLab branches before force-pushing and restore protection afterward.

This is needed when GitLab protected branches prevent synchronization.

### Success Log

The script writes successfully processed repositories to a local JSON file.

On future runs, already processed repositories can be skipped.

### Retry Logic

HTTP requests include retry logic for temporary network or service errors.

The script retries on common transient HTTP status codes and network failures.

---

## How It Works

For each GitHub repository, the script performs the following steps:

1. Checks whether the GitHub repository has commits.
2. Checks whether the corresponding GitLab project already exists.
3. Creates a new GitLab project or reuses an existing one.
4. Attempts to configure GitLab pull mirroring.
5. Checks the import status.
6. Waits for repository files to appear in GitLab.
7. Creates or updates a GitHub Actions secret containing the GitLab token.
8. Creates or updates a GitHub Actions workflow for GitHub-to-GitLab synchronization.
9. Prints a detailed summary for the repository.
10. Writes the repository to the success log if all required steps completed successfully.

---

## Project Structure

```text
GitHub_to_GitLab/
├── main.py
├── requirements.txt
├── README.md
└── migration_success_log.json
```

### Files

| File | Description |
|---|---|
| `main.py` | Main migration and synchronization script. |
| `requirements.txt` | Python dependencies. |
| `README.md` | Project documentation. |
| `migration_success_log.json` | Local log of successfully processed repositories. Generated automatically. |

---

## Requirements

- Python 3.11 or newer
- GitHub account or organization access
- GitLab account or namespace access
- GitHub personal access token
- GitLab personal access token
- Network access to GitHub and GitLab APIs

Python package dependencies:

```text
requests
PyNaCl
```

Install dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

---

## Configuration

Configuration is defined in `main.py`.

Main settings:

```python
MAX_REPOS_TO_PROCESS = 0
SUCCESS_LOG_FILE = "migration_success_log.json"
SKIP_SUCCESSFULLY_PROCESSED_REPOS = True

ENABLE_GITHUB_ACTIONS_SYNC = True
GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME = "GITLAB_TOKEN"
GITHUB_ACTIONS_SYNC_WORKFLOW_PATH = ".github/workflows/sync-to-gitlab.yml"

USE_GIT_PUSH_MIRROR = False

HTTP_REQUEST_TIMEOUT_SECONDS = 60
HTTP_MAX_RETRIES = 5
HTTP_RETRY_SLEEP_SECONDS = 10

GITHUB_TOKEN = "your-github-token"
GITLAB_TOKEN = "your-gitlab-token"

GITHUB_USER = "your-github-user-or-org"
GITLAB_NAMESPACE = "your-gitlab-namespace"
GITLAB_URL = "https://gitlab.com"
```

### Important Configuration Options

#### `MAX_REPOS_TO_PROCESS`

Controls how many repositories will be processed.

```python
MAX_REPOS_TO_PROCESS = 0
```

- `0` means no limit.
- Any positive number limits processing to that number of repositories.

#### `SKIP_SUCCESSFULLY_PROCESSED_REPOS`

When enabled, repositories already present in the success log are skipped.

```python
SKIP_SUCCESSFULLY_PROCESSED_REPOS = True
```

#### `ENABLE_GITHUB_ACTIONS_SYNC`

Controls whether the script configures GitHub Actions synchronization.

```python
ENABLE_GITHUB_ACTIONS_SYNC = True
```

#### `USE_GIT_PUSH_MIRROR`

Controls whether the generated GitHub Actions workflow uses:

```bash
git push --mirror
```

Default:

```python
USE_GIT_PUSH_MIRROR = False
```

This is recommended because GitLab may reject hidden refs such as:

```text
refs/remotes/origin/*
```

---

## Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/GitHub_to_GitLab.git
cd GitHub_to_GitLab
```

Create and activate a virtual environment:

```bash
python -m venv .venv
```

On Linux/macOS:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Usage

Edit `main.py` and configure the required values:

```python
GITHUB_TOKEN = "your-github-token"
GITLAB_TOKEN = "your-gitlab-token"
GITHUB_USER = "your-github-user-or-org"
GITLAB_NAMESPACE = "your-gitlab-namespace"
```

Run the script:

```bash
python main.py
```

The script will print detailed progress and a final report.

---

## Generated Files

### `migration_success_log.json`

The script creates this file automatically.

It stores repositories that were fully processed successfully.

Example:

```json
{
  "owner/repository": {
    "processed_at": "2026-01-01 12:00:00",
    "github_url": "https://github.com/owner/repository",
    "gitlab_url": "https://gitlab.com/group/repository",
    "gitlab_project_id": 123456,
    "import_status": "finished",
    "files_ok": true,
    "github_actions_sync_ok": true
  }
}
```

This file is local runtime state and usually should not be committed.

---

## Success Log

The success log prevents repeated processing of repositories that were already migrated and synchronized successfully.

By default, the success log file is:

```text
migration_success_log.json
```

If `SKIP_SUCCESSFULLY_PROCESSED_REPOS` is enabled, repositories listed in this file are skipped on subsequent runs.

```python
SKIP_SUCCESSFULLY_PROCESSED_REPOS = True
```

A repository is written to the success log only when all required processing steps complete successfully.

If a repository fails during processing, it is not added to the success log and can be retried later.
```
## GitHub Actions Synchronization

When enabled, the script creates a workflow file in each GitHub repository:

```text
.github/workflows/sync-to-gitlab.yml
```

The workflow:

- runs on push events;
- runs on tag events;
- runs on branch creation events;
- runs on branch deletion events;
- can be triggered manually;
- pushes GitHub branches to GitLab branches;
- pushes tags to GitLab;
- can temporarily remove GitLab branch protection and restore it afterward.

The workflow uses a GitHub Actions secret:

```text
GITLAB_TOKEN
```

This secret is created or updated automatically by the script.

---

## Required Token Permissions

### GitHub Token

The GitHub token should have permissions for:

- reading repositories;
- reading repository contents;
- writing repository contents;
- managing GitHub Actions secrets;
- creating or updating workflow files.

For fine-grained GitHub tokens, required repository permissions may include:

- Contents: Read and write
- Actions: Read and write
- Secrets: Read and write
- Metadata: Read-only

For classic tokens, scopes may include:

- `repo`
- `workflow`

### GitLab Token

The GitLab token should have permissions for:

- creating projects;
- deleting empty projects when recreation is needed;
- reading project details;
- configuring project settings;
- configuring pull mirroring if available;
- reading repository tree;
- managing protected branches if GitHub Actions sync needs to push to protected branches.

Recommended GitLab scopes:

- `api`
- `read_api`
- `read_repository`
- `write_repository`

---

## Limitations

### GitLab Pull Mirroring Availability

GitLab pull mirroring may not be available on all GitLab plans or instances.

Even if GitLab accepts the API request, pull mirroring may not actually become active.

The script verifies the mirror state after configuration.

### Existing Non-Empty Projects Are Not Deleted

If a matching GitLab project already exists and is not empty, the script does not delete it.

This is intentional to prevent accidental data loss.

### Empty Existing Projects May Be Recreated

If a matching GitLab project exists but is empty, the script may delete and recreate it.

This is done to retry import setup from a clean state.

### Protected Branches

Protected branches can prevent push synchronization.

The generated GitHub Actions workflow includes logic to temporarily unprotect branches and restore protection afterward, but this requires sufficient GitLab permissions.

### Large Repositories

Large repositories may take longer to import.

The script waits for imports and files for a limited time. If the timeout expires, manual verification may be required.

### API Rate Limits

GitHub and GitLab API rate limits may affect large migrations.

The script prints GitHub rate limit information at startup.

### Repository Features Not Migrated

This tool focuses on Git repository contents and synchronization.

It does not migrate:

- GitHub issues;
- pull requests;
- discussions;
- wiki pages;
- releases;
- GitHub Actions history;
- repository settings;
- branch rules;
- webhooks;
- secrets except the GitLab token secret required for sync.

### One-Way Synchronization

Synchronization is designed as:

```text
GitHub -> GitLab
```

Changes made directly in GitLab may be overwritten by future sync operations.

GitHub is treated as the source of truth.

### Force Push Behavior

The GitHub Actions workflow may force-push branches to GitLab.

This is required to keep GitLab aligned with GitHub, but it can overwrite divergent GitLab branch history.

---

## Security Notes

Do not commit real access tokens to the repository.

Recommended approach:

- use environment variables;
- use a local `.env` file ignored by Git;
- rotate tokens if they were ever committed or shared.

If tokens were committed to GitHub, revoke them immediately and create new ones.

Add sensitive files to `.gitignore`:

```gitignore
.env
.env.*
migration_success_log.json
```

---

## Troubleshooting

### `The project is not mirrored`

GitLab did not actually enable pull mirroring.

Possible causes:

- pull mirroring is not available on your GitLab plan;
- the GitLab instance does not allow pull mirroring;
- the token does not have enough permissions.

The script can still use GitHub Actions synchronization if enabled.

### `Files in GitLab: NOT COPIED/NOT DETECTED`

Possible causes:

- GitLab import is still running;
- GitLab import failed;
- GitLab rejected the import URL;
- GitHub token does not have access to repository contents;
- GitLab pull mirror has not run yet;
- GitHub Actions sync was not configured or has not run yet.

### GitHub Actions Secret Creation Failed

Check that the GitHub token has permissions to manage Actions secrets.

### Workflow File Creation Failed

Check that the GitHub token has:

- repository contents write permission;
- workflow permission;
- access to the default branch.

Also check whether the branch is protected.

### GitLab Project Creation Failed

Possible causes:

- namespace does not exist;
- token does not have permission to create projects;
- project path already exists;
- import URL was rejected;
- GitLab settings prohibit repository import by URL.

---

## Recommended `.gitignore`

```gitignore
# Virtual environment
.venv/
venv/
env/

# Python cache
__pycache__/
*.py[cod]
*$py.class

# Logs and local runtime files
*.log
migration_success_log.json

# Environment variables and secrets
.env
.env.*
*.pem
*.key

# IDE files
.idea/
.vscode/

# OS files
.DS_Store
Thumbs.db
```

---

## License

Copyright © Andrey Vasilyev.

This software is provided **"as is"**, without warranty of any kind, express or implied.

The author makes no guarantees regarding:

- correctness;
- reliability;
- availability;
- suitability for a particular purpose;
- uninterrupted operation;
- absence of errors;
- safety of data migration or synchronization results.

Use this software at your own risk.

### Personal Non-Commercial Use

This software is free for personal, non-commercial use.

For personal, non-commercial use, you are allowed to:

- use the software;
- copy the software;
- modify the source code for your own needs.

[![PayPal](https://img.shields.io/badge/PayPal-Buy%20Me%20a%20Coffee%20%241-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://www.paypal.com/paypalme/gscript/1)

### Commercial Use

Commercial use requires a paid license.

The license fee is:

```text
USD $20
```

This fee grants commercial use rights to one legal entity or one individual for an unlimited period of time.

For licensed commercial use, you are allowed to:

- use the software for commercial purposes;
- copy the software for internal use;
- modify the source code for internal use.

The license is not transferable unless explicitly permitted by the author.

### Payment

To purchase a commercial license, use PayPal:

[![PayPal](https://img.shields.io/badge/PayPal-Buy%20Commercial%20License-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://www.paypal.com/paypalme/gscript/20)

After payment, keep the PayPal receipt as proof of purchase.
```
