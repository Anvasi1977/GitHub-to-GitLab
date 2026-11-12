import base64
import json
import os
import re
import sys
import time
from urllib.parse import quote, urlparse
import requests
from nacl import encoding, public

# 0 = no limit.
# For example, 5 means: process only the first 5 repositories.
MAX_REPOS_TO_PROCESS = 0

# File with successfully fully processed repositories.
SUCCESS_LOG_FILE = "migration_success_log.json"

# If True, skip repositories on rerun
# that are already present in SUCCESS_LOG_FILE.
SKIP_SUCCESSFULLY_PROCESSED_REPOS = True

# Automatic GitHub -> GitLab synchronization via GitHub Actions.
# GitHub will push changes to GitLab by itself.
ENABLE_GITHUB_ACTIONS_SYNC = True

# GitHub Actions secret name that will be created in each GitHub repository.
GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME = "GITLAB_TOKEN"

# Workflow file path inside each GitHub repository.
GITHUB_ACTIONS_SYNC_WORKFLOW_PATH = ".github/workflows/sync-to-gitlab.yml"

# If True, the workflow will use git push --mirror.
# Important: git push --mirror also pushes refs/remotes/origin/*,
# and GitLab rejects such refs with the error:
# deny updating a hidden ref.
# Therefore, for GitHub Actions sync it is safer to use False.
USE_GIT_PUSH_MIRROR = False

# HTTP request resilience settings.
HTTP_REQUEST_TIMEOUT_SECONDS = 60
HTTP_MAX_RETRIES = 5
HTTP_RETRY_SLEEP_SECONDS = 10

# --- CONFIGURATION ---
GITHUB_TOKEN = "==your value=="
GITLAB_TOKEN = "==your value=="

GITHUB_USER = "==your value=="
GITLAB_NAMESPACE = "==your value=="

GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
# ---------------------


headers_gh = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "GitHub-to-GitLab-Migration-Script/2.0",
}

headers_gl = {
    "Authorization": f"Bearer {GITLAB_TOKEN}",
    "PRIVATE-TOKEN": GITLAB_TOKEN,
    "Accept": "application/json",
}


def load_success_log():
    """
    Loads the JSON log of successfully processed repositories.

    Format:
    {
      "owner/repo": {
        "processed_at": "...",
        "github_url": "...",
        "gitlab_url": "...",
        "import_status": "finished",
        "files_ok": true,
        "github_actions_sync_ok": true
      }
    }
    """
    if not os.path.exists(SUCCESS_LOG_FILE):
        return {}

    try:
        with open(SUCCESS_LOG_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        print(f"[!] Failed to read {SUCCESS_LOG_FILE}: invalid JSON")
        print(f"    └─ Error: {exc}")
        print("    An empty success log will be used.")
        return {}
    except OSError as exc:
        print(f"[!] Failed to open {SUCCESS_LOG_FILE}")
        print(f"    └─ Error: {exc}")
        print("    An empty success log will be used.")
        return {}

    if not isinstance(data, dict):
        print(f"[!] File {SUCCESS_LOG_FILE} has an unexpected format")
        print("    Expected a JSON object. An empty success log will be used.")
        return {}

    return data


def save_success_log(success_log):
    """
    Saves the JSON log of successfully processed repositories.
    """
    try:
        with open(SUCCESS_LOG_FILE, "w", encoding="utf-8") as file:
            json.dump(success_log, file, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as exc:
        print(f"[!] Failed to save {SUCCESS_LOG_FILE}")
        print(f"    └─ Error: {exc}")


def is_repo_successfully_processed(repo, success_log):
    """
    Checks whether the repository is present in the success log.
    """
    repo_full_name = repo["full_name"]
    return repo_full_name in success_log


def mark_repo_successfully_processed(
        repo,
        project,
        import_status,
        files_ok,
        github_actions_sync_ok,
        success_log,
):
    """
    Writes the repository to the success log only after full successful completion.

    Important:
    - if processing failed in the middle, this function is not called;
    - if the final status is problematic, this function is not called.
    """
    repo_full_name = repo["full_name"]

    success_log[repo_full_name] = {
        "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "github_url": repo.get("html_url"),
        "gitlab_url": project.get("web_url") if project else None,
        "gitlab_project_id": project.get("id") if project else None,
        "import_status": import_status,
        "files_ok": bool(files_ok),
        "github_actions_sync_ok": bool(github_actions_sync_ok),
    }

    save_success_log(success_log)

    print(f"    ├─ [OK] Repository written to success log: {SUCCESS_LOG_FILE}")


def filter_repos_by_success_log(repos, success_log):
    """
    Returns the list of repositories excluding already successfully processed ones.

    Also prints statistics:
    - how many were found in total;
    - how many entries exist in the success log;
    - how many will actually be skipped;
    - how many are left to process.
    """
    if not SKIP_SUCCESSFULLY_PROCESSED_REPOS:
        print("[*] Skipping successfully processed repositories is disabled")
        print(f"    ├─ SUCCESS_LOG_FILE: {SUCCESS_LOG_FILE}")
        print(f"    └─ To process: {len(repos)}")
        return repos

    skipped_repos = [
        repo for repo in repos
        if is_repo_successfully_processed(repo, success_log)
    ]

    remaining_repos = [
        repo for repo in repos
        if not is_repo_successfully_processed(repo, success_log)
    ]

    print("[*] Success log:")
    print(f"    ├─ File: {SUCCESS_LOG_FILE}")
    print(f"    ├─ GitHub repositories found: {len(repos)}")
    print(f"    ├─ Entries in success log: {len(success_log)}")
    print(f"    ├─ Will be skipped: {len(skipped_repos)}")
    print(f"    └─ Left to process: {len(remaining_repos)}")

    return remaining_repos


def get_gitlab_project(project_id):
    """
    Gets the current state of a GitLab project.
    """
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"

    response = api_get(url, headers_gl, service_name="GitLab")

    if response.status_code != 200:
        print("    ├─ [WARNING] Failed to get the current state of the GitLab project")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        return None

    return response.json()


def print_github_rate_limit():
    """
    Prints the current GitHub REST API rate limit for the token.
    """
    url = "https://api.github.com/rate_limit"
    response = api_get(url, headers_gh, service_name="GitHub")

    if response.status_code != 200:
        print("[!] Failed to get GitHub rate limit")
        print(f"    ├─ HTTP status: {response.status_code}")
        print(f"    └─ Response: {response.text}")
        return

    data = response.json()
    core = data.get("resources", {}).get("core", {})

    limit = core.get("limit")
    remaining = core.get("remaining")
    reset = core.get("reset")

    reset_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset)) if reset else "unknown"

    print("[*] GitHub API rate limit:")
    print(f"    ├─ limit: {limit}")
    print(f"    ├─ remaining: {remaining}")
    print(f"    └─ reset: {reset_text}")


def get_github_repo_public_key(repo_full_name):
    """
    Gets the GitHub repository public key for encrypting Actions secrets.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/actions/secrets/public-key"

    response = api_get(url, headers_gh, service_name="GitHub")

    if response.status_code != 200:
        print("    ├─ [ERROR] Failed to get GitHub public key for Actions secrets")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        print("    │  Check GitHub token permissions: Actions secrets read/write.")
        return None

    return response.json()


def encrypt_github_actions_secret(public_key_value, secret_value):
    """
    Encrypts a secret for GitHub Actions using a libsodium sealed box.
    """
    public_key = public.PublicKey(
        public_key_value.encode("utf-8"),
        encoding.Base64Encoder,
    )
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))

    return base64.b64encode(encrypted).decode("utf-8")


def create_or_update_github_actions_secret(repo):
    """
    Creates or updates the GITLAB_TOKEN secret in the GitHub repository.

    This secret will be used by the GitHub Actions workflow to push to GitLab.
    """
    repo_full_name = repo["full_name"]
    secret_name = GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME

    print(f"    ├─ [*] Creating/updating GitHub Actions secret: {secret_name}")

    public_key_data = get_github_repo_public_key(repo_full_name)

    if not public_key_data:
        return False

    key_id = public_key_data.get("key_id")
    public_key_value = public_key_data.get("key")

    if not key_id or not public_key_value:
        print("    ├─ [ERROR] GitHub returned an invalid public key for Actions secrets")
        print(f"    │  └─ Response: {public_key_data}")
        return False

    encrypted_value = encrypt_github_actions_secret(public_key_value, GITLAB_TOKEN)

    url = f"https://api.github.com/repos/{repo_full_name}/actions/secrets/{secret_name}"

    payload = {
        "encrypted_value": encrypted_value,
        "key_id": key_id,
    }

    response = api_put(url, headers_gh, json_local=payload, service_name="GitHub")

    if response.status_code in (201, 204):
        print(f"    ├─ [OK] GitHub Actions secret {secret_name} created/updated")
        return True

    print(f"    ├─ [ERROR] Failed to create/update GitHub Actions secret {secret_name}")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")
    print("    │  Check GitHub token permissions for managing Actions secrets.")

    return False


def build_github_to_gitlab_workflow_content(gitlab_path):
    """
    Builds the GitHub Actions workflow content for GitHub -> GitLab synchronization.

    Workflow logic:
    - does not use git push --mirror to avoid pushing refs/remotes/origin/* to GitLab;
    - pushes GitHub branches to GitLab branches via refs/remotes/origin/* -> refs/heads/*;
    - uses a force refspec because GitHub is the source of truth;
    - if a GitLab branch is protected, temporarily removes protection before push;
    - restores protection after push via trap.
    """
    gitlab_host = get_gitlab_host()
    gitlab_project_ref_value = quote(f"{GITLAB_NAMESPACE}/{gitlab_path}", safe="")

    if USE_GIT_PUSH_MIRROR:
        push_command = """echo "WARNING: git push --mirror can fail on GitLab because of hidden refs"
          git push gitlab --mirror"""
    else:
        push_command = f"""GITLAB_API_URL="{GITLAB_URL}/api/v4"
          GITLAB_PROJECT_REF="{gitlab_project_ref_value}"
          PROTECTED_BRANCHES_TO_RESTORE_FILE="$(mktemp)"

          url_encode() {{
            python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
          }}

          is_branch_protected() {{
            branch="$1"
            encoded_branch="$(url_encode "$branch")"

            status_code="$(curl --silent --output /tmp/gitlab_protected_branch.json --write-out "%{{http_code}}" \\
              --header "PRIVATE-TOKEN: ${{{{ secrets.{GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME} }}}}" \\
              "$GITLAB_API_URL/projects/$GITLAB_PROJECT_REF/protected_branches/$encoded_branch")"

            if [ "$status_code" = "200" ]; then
              return 0
            fi

            return 1
          }}

          unprotect_branch() {{
            branch="$1"
            encoded_branch="$(url_encode "$branch")"

            echo "Temporarily unprotecting GitLab branch: $branch"

            curl --silent --show-error --fail --request DELETE \\
              --header "PRIVATE-TOKEN: ${{{{ secrets.{GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME} }}}}" \\
              "$GITLAB_API_URL/projects/$GITLAB_PROJECT_REF/protected_branches/$encoded_branch"
          }}

          protect_branch() {{
            branch="$1"

            echo "Restoring protection for GitLab branch: $branch"

            curl --silent --show-error --fail --request POST \\
              --header "PRIVATE-TOKEN: ${{{{ secrets.{GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME} }}}}" \\
              --form "name=$branch" \\
              --form "push_access_level=40" \\
              --form "merge_access_level=40" \\
              --form "unprotect_access_level=40" \\
              --form "allow_force_push=false" \\
              "$GITLAB_API_URL/projects/$GITLAB_PROJECT_REF/protected_branches" \\
              || echo "WARNING: failed to restore protection for branch: $branch"
          }}

          restore_protected_branches() {{
            if [ ! -s "$PROTECTED_BRANCHES_TO_RESTORE_FILE" ]; then
              echo "No protected branches need to be restored"
              rm -f "$PROTECTED_BRANCHES_TO_RESTORE_FILE"
              return 0
            fi

            echo "Restoring protected branches..."

            while IFS= read -r branch; do
              if [ -n "$branch" ]; then
                protect_branch "$branch"
              fi
            done < "$PROTECTED_BRANCHES_TO_RESTORE_FILE"

            rm -f "$PROTECTED_BRANCHES_TO_RESTORE_FILE"
          }}

          trap restore_protected_branches EXIT

          echo "Pushing branches from GitHub to GitLab..."

          while IFS= read -r branch; do
            if [ "$branch" = "HEAD" ]; then
              continue
            fi

            if is_branch_protected "$branch"; then
              echo "GitLab branch is protected: $branch"
              echo "$branch" >> "$PROTECTED_BRANCHES_TO_RESTORE_FILE"
              unprotect_branch "$branch"
            else
              echo "GitLab branch is not protected: $branch"
            fi

            echo "Force pushing branch: $branch"
            git push gitlab "+refs/remotes/origin/$branch:refs/heads/$branch"
          done < <(git for-each-ref --format='%(refname:strip=3)' refs/remotes/origin)

          echo "Pushing tags from GitHub to GitLab..."
          git push gitlab --tags"""

    return f"""name: Sync to GitLab

on:
  push:
    branches:
      - "**"
    tags:
      - "**"
  create:
  delete:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: sync-to-gitlab-${{{{ github.repository }}}}
  cancel-in-progress: false

jobs:
  sync-to-gitlab:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout full repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Configure Git
        run: |
          git config --global user.name "GitHub to GitLab Sync"
          git config --global user.email "github-actions@github.com"

      - name: Push to GitLab
        run: |
          git remote add gitlab "https://oauth2:${{{{ secrets.{GITHUB_ACTIONS_GITLAB_TOKEN_SECRET_NAME} }}}}@{gitlab_host}/{GITLAB_NAMESPACE}/{gitlab_path}.git"
          {push_command}
"""


def get_github_file_info(repo_full_name, file_path, branch):
    """
    Gets information about a file in a GitHub repository.

    Returns:
    - dict if the file exists;
    - None if the file does not exist or could not be read.
    """
    encoded_path = quote(file_path, safe="/")
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{encoded_path}"

    response = api_get(
        url,
        headers_gh,
        params={"ref": branch},
        service_name="GitHub",
    )

    if response.status_code == 200:
        data = response.json()

        if isinstance(data, dict):
            return data

        print(f"    ├─ [WARNING] GitHub returned an unexpected format for file {file_path}")
        print(f"    │  └─ Response: {data}")
        return None

    if response.status_code == 404:
        return None

    print(f"    ├─ [WARNING] Failed to check whether file {file_path} exists in GitHub")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")

    return None


def create_or_update_github_file(repo_full_name, branch, file_path, content, commit_message):
    """
    Creates or updates a file in a GitHub repository via the Contents API.

    If the file already exists and its content matches the desired content,
    no commit is created, and the operation is considered successful.
    """
    encoded_path = quote(file_path, safe="/")
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{encoded_path}"

    existing_file = get_github_file_info(repo_full_name, file_path, branch)
    existing_sha = None

    if existing_file:
        existing_sha = existing_file.get("sha")
        existing_encoded_content = existing_file.get("content") or ""
        existing_encoded_content = existing_encoded_content.replace("\n", "")

        try:
            existing_content = base64.b64decode(existing_encoded_content).decode("utf-8")
        except Exception as exc:
            print(f"    ├─ [WARNING] Failed to decode existing file {file_path}")
            print(f"    │  └─ Error: {exc}")
            existing_content = None

        if existing_content == content:
            print(f"    ├─ [OK] GitHub file {file_path} is already up to date; no update required")
            return True

    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    payload = {
        "message": commit_message,
        "content": encoded_content,
        "branch": branch,
    }

    if existing_sha:
        payload["sha"] = existing_sha

    response = api_put(url, headers_gh, json_local=payload, service_name="GitHub")

    if response.status_code in (200, 201):
        action = "updated" if existing_sha else "created"
        print(f"    ├─ [OK] GitHub file {file_path} {action}")
        return True

    if response.status_code == 422:
        print(f"    ├─ [WARNING] GitHub rejected creating/updating file {file_path}")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        print("    │  Possible reasons:")
        print("    │  ├─ the workflow file already exists and does not require changes")
        print("    │  ├─ the branch is protected")
        print("    │  ├─ the GitHub token does not have the workflow scope")
        print("    │  └─ GitHub did not allow changing a file in .github/workflows")
        return False

    print(f"    ├─ [ERROR] Failed to create/update GitHub file {file_path}")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")
    print("    │  Check GitHub token permissions: Contents read/write and Workflow.")

    return False


def create_or_update_github_actions_sync_workflow(repo):
    """
    Creates or updates the GitHub Actions workflow for push synchronization to GitLab.
    """
    repo_full_name = repo["full_name"]
    repo_name = repo["name"]
    default_branch = repo.get("default_branch")

    if not default_branch:
        print("    ├─ [INFO] GitHub Actions workflow was not created: the repository has no default_branch")
        return False

    gitlab_path = sanitize_gitlab_path(repo_name)

    print("    ├─ [*] Creating/updating GitHub Actions workflow for GitHub -> GitLab...")
    print(f"    │  ├─ GitHub repo name: {repo_name}")
    print(f"    │  └─ GitLab project path for workflow: {gitlab_path}")

    workflow_content = build_github_to_gitlab_workflow_content(gitlab_path)

    return create_or_update_github_file(
        repo_full_name=repo_full_name,
        branch=default_branch,
        file_path=GITHUB_ACTIONS_SYNC_WORKFLOW_PATH,
        content=workflow_content,
        commit_message="Add GitHub to GitLab sync workflow",
    )


def setup_github_actions_sync(repo):
    """
    Fully configures GitHub Actions synchronization GitHub -> GitLab:
    - creates/updates the GITLAB_TOKEN Actions secret;
    - creates/updates the sync-to-gitlab.yml workflow.
    """
    if not ENABLE_GITHUB_ACTIONS_SYNC:
        # noinspection unreachable-code
        print("    ├─ [INFO] GitHub Actions sync is disabled by ENABLE_GITHUB_ACTIONS_SYNC")
        return False

    print("    ├─ [*] Configuring GitHub Actions synchronization GitHub -> GitLab...")

    secret_ok = create_or_update_github_actions_secret(repo)

    if not secret_ok:
        print("    ├─ [ERROR] GitHub Actions sync was not configured: failed to create secret")
        return False

    workflow_ok = create_or_update_github_actions_sync_workflow(repo)

    if not workflow_ok:
        print("    ├─ [ERROR] GitHub Actions sync was not configured: failed to create workflow")
        return False

    print("    ├─ [OK] GitHub Actions synchronization GitHub -> GitLab configured")
    return True


def print_gitlab_mirror_state(project_id):
    """
    Prints the actual state of the mirror/import fields of the GitLab project.
    """
    project = get_gitlab_project(project_id)

    if not project:
        return None

    print("    ├─ [*] Actual mirror/import state in GitLab:")
    print(f"    │  ├─ mirror: {project.get('mirror')}")
    print(f"    │  ├─ import_status: {project.get('import_status')}")
    print(f"    │  ├─ import_error: {project.get('import_error')}")
    print(f"    │  ├─ import_url set: {'YES' if project.get('import_url') else 'NO'}")
    print(f"    │  ├─ mirror_user_id: {project.get('mirror_user_id')}")
    print(f"    │  ├─ mirror_trigger_builds: {project.get('mirror_trigger_builds')}")
    print(f"    │  ├─ only_mirror_protected_branches: {project.get('only_mirror_protected_branches')}")
    print(f"    │  └─ mirror_overwrites_diverged_branches: {project.get('mirror_overwrites_diverged_branches')}")

    return project


def print_gitlab_project_permissions(project):
    """
    Prints the permissions of the current GitLab token in the project.
    """
    permissions = project.get("permissions") or {}

    project_access = permissions.get("project_access") or {}
    group_access = permissions.get("group_access") or {}

    project_level = project_access.get("access_level")
    group_level = group_access.get("access_level")

    print("    ├─ [*] Current GitLab token permissions in the project:")
    print(f"    │  ├─ project_access: {project_level}")
    print(f"    │  └─ group_access: {group_level}")

    max_level = max(
        project_level or 0,
        group_level or 0,
    )

    if max_level >= 50:
        print("    │  └─ Access level: Owner")
    elif max_level >= 40:
        print("    │  └─ Access level: Maintainer")
    elif max_level >= 30:
        print("    │  └─ Access level: Developer")
    elif max_level >= 20:
        print("    │  └─ Access level: Reporter")
    elif max_level >= 10:
        print("    │  └─ Access level: Guest")
    else:
        print("    │  └─ Access level is not defined or missing")

    return max_level


def check_gitlab_current_user():
    """
    Checks which GitLab user is used for API requests.
    """
    print("[*] Checking the current GitLab token user...")

    url = f"{GITLAB_URL}/api/v4/user"
    response = api_get(url, headers_gl, service_name="GitLab")

    if response.status_code != 200:
        print("    ├─ [ERROR] GitLab token failed the /user check")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        return None

    user = response.json()

    print("    ├─ [OK] GitLab token is valid")
    print(f"    │  ├─ id: {user.get('id')}")
    print(f"    │  ├─ username: {user.get('username')}")
    print(f"    │  ├─ name: {user.get('name')}")
    print(f"    │  └─ state: {user.get('state')}")

    return user


def fatal(message):
    print(f"[CRITICAL ERROR] {message}")
    sys.exit(1)


def api_request(method, url, headers, params=None, json_local=None, data=None, service_name="API"):
    """
    Performs an HTTP request with retries.

    This is needed for bulk migration because GitHub/GitLab/proxy may
    temporarily drop the connection.
    """
    last_exception = None
    last_response = None

    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_local,
                data=data,
                timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
            )

            last_response = response

            if response.status_code in (429, 500, 502, 503, 504):
                print(
                    f"[!] Temporary {service_name} error: "
                    f"HTTP {response.status_code}, attempt {attempt}/{HTTP_MAX_RETRIES}"
                )
                print(f"    └─ URL: {url}")

                if attempt < HTTP_MAX_RETRIES:
                    time.sleep(HTTP_RETRY_SLEEP_SECONDS * attempt)
                    continue

            return response

        except (
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
        ) as exc:
            last_exception = exc

            print(
                f"[!] Network error while contacting {service_name}, "
                f"attempt {attempt}/{HTTP_MAX_RETRIES}"
            )
            print(f"    ├─ URL: {url}")
            print(f"    └─ Error: {exc}")

            if attempt < HTTP_MAX_RETRIES:
                time.sleep(HTTP_RETRY_SLEEP_SECONDS * attempt)
                continue

        except requests.RequestException as exc:
            last_exception = exc

            print(
                f"[!] requests error while contacting {service_name}, "
                f"attempt {attempt}/{HTTP_MAX_RETRIES}"
            )
            print(f"    ├─ URL: {url}")
            print(f"    └─ Error: {exc}")

            if attempt < HTTP_MAX_RETRIES:
                time.sleep(HTTP_RETRY_SLEEP_SECONDS * attempt)
                continue

    if last_response is not None:
        return last_response

    fatal(f"Network error while contacting {service_name} after {HTTP_MAX_RETRIES} attempts: {last_exception}")


def api_get(url, headers, params=None, service_name="API"):
    return api_request(
        method="GET",
        url=url,
        headers=headers,
        params=params,
        service_name=service_name,
    )


def api_post(url, headers, json_local=None, data=None, service_name="API"):
    return api_request(
        method="POST",
        url=url,
        headers=headers,
        json_local=json_local,
        data=data,
        service_name=service_name,
    )


def api_put(url, headers, json_local=None, data=None, service_name="API"):
    return api_request(
        method="PUT",
        url=url,
        headers=headers,
        json_local=json_local,
        data=data,
        service_name=service_name,
    )


def api_delete(url, headers, service_name="API"):
    return api_request(
        method="DELETE",
        url=url,
        headers=headers,
        service_name=service_name,
    )


def gitlab_project_ref(namespace, path):
    return quote(f"{namespace}/{path}", safe="")


def get_gitlab_host():
    """
    Returns the GitLab host without the URL scheme.

    Examples:
    - https://gitlab.com -> gitlab.com
    - https://gitlab.example.com -> gitlab.example.com
    """
    parsed_url = urlparse(GITLAB_URL)

    if parsed_url.netloc:
        return parsed_url.netloc

    return parsed_url.path.rstrip("/")


def make_github_import_url(repo_full_name):
    """
    URL for GitHub -> GitLab import and pull mirror.
    """
    encoded_token = quote(GITHUB_TOKEN, safe="")
    return f"https://x-access-token:{encoded_token}@github.com/{repo_full_name}.git"


def sanitize_gitlab_path(name):
    """
    Makes the repository name a valid GitLab path.
    """
    path = name.strip("_-. ")
    path = re.sub(r"[^a-zA-Z0-9._-]+", "-", path)
    path = re.sub(r"-{2,}", "-", path)
    path = path.strip("_-.")

    if not path:
        path = "repository"

    return path


def get_github_repos():
    """
    Gets all owned GitHub repositories available to the current token.
    """
    repos = []
    page = 1
    url = "https://api.github.com/user/repos"

    print("[*] Connecting to GitHub API...")

    while True:
        params = {
            "per_page": 100,
            "page": page,
            "sort": "full_name",
            "direction": "asc",
            "affiliation": "owner",
        }

        response = api_get(url, headers_gh, params=params, service_name="GitHub")

        if response.status_code != 200:
            print("[!] GitHub API returned an error.")
            print(f"    ├─ HTTP status: {response.status_code}")
            print(f"    └─ Response: {response.text}")
            fatal("Failed to get the list of GitHub repositories.")

        data = response.json()

        if not isinstance(data, list):
            print("[!] GitHub returned an unexpected data format.")
            print(f"    └─ Response: {data}")
            fatal("Expected a list of repositories.")

        if not data:
            break

        filtered_data = [
            repo for repo in data
            if repo.get("owner", {}).get("login", "").lower() == GITHUB_USER.lower()
        ]

        repos.extend(filtered_data)
        print(
            f"    ├─ Received page {page}: "
            f"{len(data)} repositories, after GITHUB_USER filter: {len(filtered_data)}"
        )
        page += 1

    return repos


def check_github_repository_has_commits(repo):
    """
    Checks whether the source GitHub repository has commits.
    """
    repo_full_name = repo["full_name"]
    default_branch = repo.get("default_branch")

    if not default_branch:
        print("    ├─ [INFO] The GitHub repository has no default_branch")
        print("    │  The source GitHub repository is probably empty.")
        return False

    url = f"https://api.github.com/repos/{repo_full_name}/commits"

    response = api_get(
        url,
        headers_gh,
        params={
            "sha": default_branch,
            "per_page": 1,
        },
        service_name="GitHub",
    )

    if response.status_code == 409:
        print("    ├─ [INFO] GitHub reports that the repository is empty")
        return False

    if response.status_code != 200:
        print("    ├─ [WARNING] Failed to check commits in GitHub")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        print("    │  Continuing processing, assuming the repository is not empty.")
        return True

    commits = response.json()

    if not commits:
        print("    ├─ [INFO] No commits found in the GitHub repository")
        return False

    print(f"    ├─ [OK] The GitHub repository has commits, default_branch={default_branch}")
    return True


def get_gitlab_namespace_id():
    """
    Gets the GitLab namespace ID.
    """
    url = f"{GITLAB_URL}/api/v4/namespaces"

    print("[*] Checking GitLab namespace...")

    response = api_get(
        url,
        headers_gl,
        params={"search": GITLAB_NAMESPACE},
        service_name="GitLab",
    )

    if response.status_code != 200:
        print("[!] Failed to get namespaces from GitLab.")
        print(f"    ├─ HTTP status: {response.status_code}")
        print(f"    └─ Response: {response.text}")
        fatal("Check the GitLab token, namespace, and access permissions.")

    namespaces = response.json()

    if not isinstance(namespaces, list):
        print("[!] GitLab returned an unexpected namespaces format.")
        print(f"    └─ Response: {namespaces}")
        fatal("Expected a list of namespaces.")

    for namespace in namespaces:
        if namespace.get("path") == GITLAB_NAMESPACE or namespace.get("full_path") == GITLAB_NAMESPACE:
            print(f"    └─ [OK] Found namespace '{GITLAB_NAMESPACE}', id={namespace['id']}")
            return namespace["id"]

    print(f"[!] Namespace '{GITLAB_NAMESPACE}' was not found.")
    print("    Similar namespaces found:")

    for namespace in namespaces:
        print(
            f"    ├─ id={namespace.get('id')} "
            f"path={namespace.get('path')} "
            f"full_path={namespace.get('full_path')}"
        )

    fatal("Specify the correct GITLAB_NAMESPACE.")


def get_existing_gitlab_project(gitlab_path):
    """
    Checks whether a project exists in GitLab.
    """
    project_ref = gitlab_project_ref(GITLAB_NAMESPACE, gitlab_path)
    url = f"{GITLAB_URL}/api/v4/projects/{project_ref}"

    response = api_get(url, headers_gl, service_name="GitLab")

    if response.status_code == 200:
        return response.json()

    if response.status_code == 404:
        return None

    print("[!] Error while checking an existing project in GitLab.")
    print(f"    ├─ HTTP status: {response.status_code}")
    print(f"    └─ Response: {response.text}")
    return None


def is_gitlab_project_empty(project):
    """
    Checks whether a GitLab project is empty.

    Logic:
    - if default_branch is missing, the project is considered empty;
    - if repository/tree returns 404, the project is considered empty;
    - if tree is an empty list, the project is considered empty;
    - if tree contains items, the project is NOT empty.
    """
    project_id = project["id"]
    project_name = project.get("path_with_namespace") or project.get("name") or project_id

    actual_project = get_gitlab_project(project_id)

    if not actual_project:
        print("    ├─ [WARNING] Failed to check whether the GitLab project is empty")
        print(f"    │  Project: {project_name}")
        return False

    default_branch = actual_project.get("default_branch")

    if not default_branch:
        print("    ├─ [INFO] GitLab project is empty: default_branch is missing")
        return True

    url = f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/tree"

    response = api_get(
        url,
        headers_gl,
        params={
            "ref": default_branch,
            "per_page": 1,
        },
        service_name="GitLab",
    )

    if response.status_code == 404:
        print("    ├─ [INFO] GitLab project is empty: repository/tree returned 404")
        return True

    if response.status_code != 200:
        print("    ├─ [WARNING] Failed to check the GitLab project tree")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        print("    │  The project will NOT be deleted to avoid data loss.")
        return False

    tree = response.json()

    if not tree:
        print("    ├─ [INFO] GitLab project is empty: repository tree is empty")
        return True

    print("    ├─ [INFO] GitLab project is not empty; deletion is not required")
    return False


def delete_gitlab_project(project):
    """
    Deletes a GitLab project.

    GitLab may delete a project asynchronously:
    the API has already accepted DELETE, but the project may still be visible via GET for some time.
    """
    project_id = project["id"]
    project_name = project.get("path_with_namespace") or project.get("name") or project_id

    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"

    print(f"    ├─ [*] Deleting empty GitLab project: {project_name}")

    response = api_delete(url, headers_gl, service_name="GitLab")

    if response.status_code in (200, 202, 204):
        print("    ├─ [OK] GitLab accepted the project deletion request")
        print(f"    │  └─ Deletion HTTP status: {response.status_code}")
        return True

    if response.status_code == 400 and "already been marked for deletion" in response.text:
        print("    ├─ [INFO] GitLab project was already marked for deletion earlier")
        print("    │  Continuing to wait for the source path to be released.")
        return True

    print("    ├─ [ERROR] Failed to delete the empty GitLab project")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")
    return False


def wait_for_gitlab_project_deleted(gitlab_path, timeout_seconds=180, interval_seconds=10):
    """
    Waits until the source GitLab path is released after project deletion.

    GitLab may not delete the project instantly, but instead rename it to:
    <path>-deletion_scheduled-<id>

    In this case, the full project still exists, but the source path can already
    be used to create a new project.
    """
    started_at = time.time()

    print("    ├─ [*] Waiting for the GitLab path to be released after project deletion...")

    attempt = 1
    expected_full_path = f"{GITLAB_NAMESPACE}/{gitlab_path}"

    while time.time() - started_at < timeout_seconds:
        existing_project = get_existing_gitlab_project(gitlab_path)

        if existing_project is None:
            print("    ├─ [OK] Source GitLab path has been released")
            return True

        elapsed = int(time.time() - started_at)
        project_id = existing_project.get("id")
        project_path = existing_project.get("path")
        project_full_path = existing_project.get("path_with_namespace") or existing_project.get("name")
        default_branch = existing_project.get("default_branch")
        import_status = existing_project.get("import_status")
        marked_for_deletion_on = existing_project.get("marked_for_deletion_on")
        pending_delete = existing_project.get("pending_delete")

        path_changed_to_deletion_scheduled = (
                project_full_path != expected_full_path
                and project_full_path is not None
                and "-deletion_scheduled-" in project_full_path
        )

        if path_changed_to_deletion_scheduled:
            print("    ├─ [OK] GitLab renamed the deleted project to deletion_scheduled")
            print(f"    │  ├─ old path: {expected_full_path}")
            print(f"    │  └─ current path: {project_full_path}")
            print("    │  The source path can be used to create a new project.")
            return True

        print(f"    │  ├─ Attempt {attempt}: source path is still occupied, elapsed {elapsed} sec.")
        print(f"    │  │  ├─ id: {project_id}")
        print(f"    │  │  ├─ path: {project_path}")
        print(f"    │  │  ├─ full_path: {project_full_path}")
        print(f"    │  │  ├─ default_branch: {default_branch}")
        print(f"    │  │  ├─ import_status: {import_status}")
        print(f"    │  │  ├─ marked_for_deletion_on: {marked_for_deletion_on}")
        print(f"    │  │  └─ pending_delete: {pending_delete}")
        print(f"    │  └─ Retrying in {interval_seconds} sec.")

        attempt += 1
        time.sleep(interval_seconds)

    print("    ├─ [ERROR] GitLab path was not released within the timeout")
    print(f"    │  ├─ path: {expected_full_path}")
    print(f"    │  ├─ timeout_seconds: {timeout_seconds}")
    print("    │  └─ A new project with the same path cannot be created yet.")

    return False


def create_gitlab_project_from_github(repo, namespace_id):
    """
    Creates a GitLab project and immediately sets import_url.

    If the GitLab project already exists and is empty:
    - deletes it;
    - waits for deletion;
    - recreates it immediately with import_url and mirror=True.

    If the GitLab project already exists and is NOT empty:
    - does not delete it;
    - returns the existing project.
    """
    repo_name = repo["name"]
    repo_full_name = repo["full_name"]
    gitlab_path = sanitize_gitlab_path(repo_name)
    import_url = make_github_import_url(repo_full_name)

    existing_project = get_existing_gitlab_project(gitlab_path)

    if existing_project:
        print("    ├─ [INFO] Project already exists in GitLab")

        if is_gitlab_project_empty(existing_project):
            print("    ├─ [INFO] Existing GitLab project is empty and will be recreated")

            deleted = delete_gitlab_project(existing_project)

            if not deleted:
                print("    ├─ [ERROR] Recreation is impossible: failed to delete the project")
                return None, False

            deleted_confirmed = wait_for_gitlab_project_deleted(
                gitlab_path,
                timeout_seconds=180,
                interval_seconds=10,
            )

            if not deleted_confirmed:
                print("    ├─ [ERROR] Recreation is impossible: GitLab did not confirm project deletion")
                print("    │  Possible reasons:")
                print("    │  ├─ GitLab is deleting the project asynchronously longer than usual")
                print("    │  ├─ the project is in pending deletion")
                print("    │  ├─ the path is still reserved by GitLab")
                print("    │  └─ you need to run the script again later")
                return None, False

            print("    ├─ [INFO] Empty GitLab project deleted; creating it again with import_url and mirror=True")
        else:
            print("    ├─ [INFO] Existing GitLab project is not empty; recreation will not be performed")
            return existing_project, False

    url = f"{GITLAB_URL}/api/v4/projects"

    payload = {
        "name": repo_name,
        "path": gitlab_path,
        "namespace_id": namespace_id,
        "visibility": "private",
        "import_url": import_url,
        "mirror": True,
        "mirror_trigger_builds": False,
        "mirror_overwrites_diverged_branches": True,
    }

    response = api_post(url, headers_gl, json_local=payload, service_name="GitLab")

    if response.status_code == 201:
        print("    ├─ [OK] Project created in GitLab; GitHub import started")
        return response.json(), True

    print("    ├─ [ERROR] Failed to create the GitLab project")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")

    if response.status_code in (400, 403):
        print("    │")
        print("    │  Possible reasons:")
        print("    │  ├─ Pull mirroring is not available on your GitLab")
        print("    │  ├─ GitLab token does not have the required permissions")
        print("    │  ├─ GitHub token does not have access to the source repository")
        print("    │  ├─ import_url was rejected by GitLab settings")
        print("    │  └─ a project with a similar path already exists")

    return None, False


def verify_pull_mirror_enabled(project_id):
    """
    Checks whether GitLab actually enabled pull mirror.

    Important:
    HTTP 200 on PUT /projects/:id does not guarantee that mirror was actually enabled.
    """
    project = get_gitlab_project(project_id)

    if not project:
        return False

    mirror_enabled = project.get("mirror")
    import_url = project.get("import_url")

    print("    ├─ [*] Checking the actual pull mirror state...")
    print(f"    │  ├─ mirror: {mirror_enabled}")
    print(f"    │  └─ import_url set: {'YES' if import_url else 'NO'}")

    if mirror_enabled is True:
        print("    ├─ [OK] GitLab confirmed that the project is a pull mirror")
        return True

    print("    ├─ [PROBLEM] GitLab did NOT confirm pull mirror activation")
    print("    │  The next manual mirror run will most likely return:")
    print("    │  The project is not mirrored")

    return False


def configure_pull_mirror(project, repo):
    """
    Configures one-way synchronization GitHub -> GitLab.

    Important:
    HTTP 200 from GitLab does not mean that mirror was actually enabled.
    Therefore, after PUT we must reread the project and check the mirror field.
    """
    project_id = project["id"]
    repo_full_name = repo["full_name"]
    import_url = make_github_import_url(repo_full_name)

    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"

    payload = {
        "import_url": import_url,
        "mirror": True,
        "mirror_trigger_builds": False,
        "mirror_overwrites_diverged_branches": True,
    }

    print("    ├─ [*] Configuring pull mirror GitHub -> GitLab...")

    response = api_put(url, headers_gl, json_local=payload, service_name="GitLab")

    if response.status_code != 200:
        print("    ├─ [WARNING] Failed to configure pull mirror")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")

        if response.status_code in (400, 403):
            print("    │")
            print("    │  Possible reasons:")
            print("    │  ├─ Pull mirroring is not available on your GitLab")
            print("    │  ├─ GitLab token has insufficient permissions")
            print("    │  ├─ GitLab forbids import by URL")
            print("    │  └─ GitHub token does not have access to this repository")

        return False

    print("    ├─ [INFO] GitLab accepted the pull mirror configuration request")

    mirror_enabled = verify_pull_mirror_enabled(project_id)

    if not mirror_enabled:
        print("    ├─ [PROBLEM] Pull mirror was not actually enabled")
        print("    │  Possible reason: pull mirroring is not available on your GitLab plan/instance.")
        return False

    return True


def trigger_pull_mirror(project):
    """
    Attempts to manually trigger pull mirror.

    If GitLab responds with "The project is not mirrored", it means mirror is not actually
    enabled, even if the previous PUT returned HTTP 200.
    """
    project_id = project["id"]
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}/mirror/pull"

    print("    ├─ [*] Attempting to manually trigger pull mirror...")

    response = api_post(url, headers_gl, service_name="GitLab")

    if response.status_code in (200, 201, 202, 204):
        print("    ├─ [OK] Pull mirror triggered manually")
        return True

    print("    ├─ [WARNING] Manual pull mirror trigger was not completed")
    print(f"    │  ├─ HTTP status: {response.status_code}")
    print(f"    │  └─ Response: {response.text}")

    if "The project is not mirrored" in response.text:
        print("    │")
        print("    │  [PROBLEM] GitLab says that the project is not a mirror.")
        print("    │  This means automatic GitHub -> GitLab synchronization is not enabled.")
        return False

    print("    │  GitLab may perform synchronization later by schedule if mirror is actually enabled.")

    return False


def wait_for_import(project_id, timeout_seconds=600, interval_seconds=10):
    """
    Checks the GitLab import status.

    Returns a string:
    - finished;
    - failed;
    - none;
    - timeout;
    - unknown_error.
    """
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"
    started_at = time.time()

    print("    ├─ [*] Checking import status...")

    last_status = None

    while time.time() - started_at < timeout_seconds:
        response = api_get(url, headers_gl, service_name="GitLab")

        if response.status_code != 200:
            print("    ├─ [WARNING] Failed to check import status")
            print(f"    │  ├─ HTTP status: {response.status_code}")
            print(f"    │  └─ Response: {response.text}")
            return "unknown_error"

        project = response.json()
        status = project.get("import_status")
        import_error = project.get("import_error")

        if status != last_status:
            print(f"    │  ├─ import_status: {status}")
            last_status = status

        if status == "finished":
            print("    ├─ [OK] Import completed")
            return "finished"

        if status == "failed":
            print("    ├─ [ERROR] Import failed")
            print(f"    │  └─ import_error: {import_error}")
            return "failed"

        if status in (None, "none"):
            print("    ├─ [INFO] No active import detected")
            return "none"

        time.sleep(interval_seconds)

    print("    ├─ [WARNING] Import wait timeout expired")
    return "timeout"


def get_gitlab_default_branch(project):
    """
    Gets the default_branch of the GitLab project.
    """
    project_id = project["id"]
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"

    response = api_get(url, headers_gl, service_name="GitLab")

    if response.status_code != 200:
        print("    ├─ [WARNING] Failed to get GitLab default_branch")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        return None

    return response.json().get("default_branch")


def check_gitlab_repository_has_files(project):
    """
    Checks whether files or directories have appeared in the GitLab repository.
    """
    project_id = project["id"]
    default_branch = get_gitlab_default_branch(project)

    if not default_branch:
        print("    ├─ [CHECK] GitLab does not have default_branch yet")
        print("    │  This means the repository is empty or import/mirror has not completed yet.")
        return False

    url = f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/tree"

    response = api_get(
        url,
        headers_gl,
        params={
            "ref": default_branch,
            "per_page": 1,
        },
        service_name="GitLab",
    )

    if response.status_code != 200:
        print("    ├─ [PROBLEM] Failed to get GitLab file tree")
        print(f"    │  ├─ HTTP status: {response.status_code}")
        print(f"    │  └─ Response: {response.text}")
        return False

    tree = response.json()

    if not tree:
        print("    ├─ [CHECK] GitLab repository is still empty")
        print("    │  Files from GitHub have not been detected yet.")
        return False

    print(f"    ├─ [OK] Files detected in GitLab, default_branch={default_branch}")
    return True


def wait_for_gitlab_files(project, timeout_seconds=600, interval_seconds=15):
    """
    Waits until default_branch and files appear in GitLab.
    """
    started_at = time.time()

    print("    ├─ [*] Waiting for files to appear in GitLab...")

    while time.time() - started_at < timeout_seconds:
        if check_gitlab_repository_has_files(project):
            return True

        print(f"    │  ├─ Files not detected yet, retrying in {interval_seconds} sec.")
        time.sleep(interval_seconds)

    print("    ├─ [PROBLEM] Files did not appear in GitLab within the timeout")
    return False


def print_project_summary(
        repo,
        project,
        import_status,
        files_ok,
        mirror_ok,
        github_actions_sync_ok,
        github_has_commits,
        was_created,
):
    """
    Prints the summary for one repository.
    """
    repo_name = repo["name"]
    github_url = repo.get("html_url")
    gitlab_url = project.get("web_url") if project else None

    print("    ├─ [SUMMARY]")
    print(f"    │  ├─ GitHub: {github_url}")
    print(f"    │  ├─ GitLab: {gitlab_url}")
    print(f"    │  ├─ GitLab project: {'CREATED' if was_created else 'ALREADY EXISTED'}")
    print(f"    │  ├─ Commits in GitHub: {'YES' if github_has_commits else 'NO / REPOSITORY EMPTY'}")
    print(f"    │  ├─ Import status: {import_status}")
    print(f"    │  ├─ Files in GitLab: {'OK' if files_ok else 'NOT COPIED/NOT DETECTED'}")
    print(f"    │  ├─ GitLab pull mirror: {'OK' if mirror_ok else 'NOT CONFIGURED/UNAVAILABLE'}")
    print(f"    │  └─ GitHub Actions sync GitHub -> GitLab: {'OK' if github_actions_sync_ok else 'NOT CONFIGURED'}")

    if github_has_commits and not files_ok:
        print("    │")
        print(f"    │  [!] Repository '{repo_name}' requires file transfer verification.")
        print("    │  Possible reasons:")
        print("    │  ├─ GitLab has not performed pull mirror yet")
        print("    │  ├─ GitLab rejected import_url")
        print("    │  ├─ import failed")
        print("    │  ├─ GitHub token does not have access to repository contents")
        print("    │  └─ GitLab project already existed, was empty, but could not pull code")

    if not mirror_ok and github_actions_sync_ok:
        print("    │")
        print(f"    │  [INFO] GitLab pull mirror for '{repo_name}' is not configured.")
        print("    │  But automatic synchronization will be performed via GitHub Actions.")

    if not github_actions_sync_ok:
        print("    │")
        print(f"    │  [!] GitHub Actions synchronization is not configured for repository '{repo_name}'.")
        print("    │  This means automatic GitHub -> GitLab synchronization is not configured.")

    if not github_has_commits and files_ok:
        print("    │")
        print("    │  [INFO] The source GitHub repository is empty.")
        print("    │  The absence of files in GitLab is considered normal for it.")


def main():
    print("=" * 80)
    print(" GITHUB -> GITLAB MIGRATION AND SYNCHRONIZATION SETUP ")
    print("=" * 80)
    print(f"GitLab URL: {GITLAB_URL}")
    print(f"GitLab namespace: {GITLAB_NAMESPACE}")
    print(f"GitHub user/org hint: {GITHUB_USER}")
    print(f"Max repos to process: {'no limit' if MAX_REPOS_TO_PROCESS == 0 else MAX_REPOS_TO_PROCESS}")
    print(f"GitHub Actions sync: {'enabled' if ENABLE_GITHUB_ACTIONS_SYNC else 'disabled'}")
    print(
        f"Git push mode: "
        f"{'--mirror' if USE_GIT_PUSH_MIRROR else 'force branches refs + tags with protected branch restore'}"
    )
    print("=" * 80)

    print_github_rate_limit()
    check_gitlab_current_user()

    gh_repos = get_github_repos()

    found_repos = len(gh_repos)

    print(f"[+] GitHub repositories found: {found_repos}")

    if found_repos == 0:
        print("[!] No repositories available for migration.")
        return

    success_log = load_success_log()
    gh_repos = filter_repos_by_success_log(gh_repos, success_log)

    remaining_repos_before_limit = len(gh_repos)

    if remaining_repos_before_limit == 0:
        print("[OK] All found repositories have already been successfully processed.")
        return

    if MAX_REPOS_TO_PROCESS > 0:
        gh_repos = gh_repos[:MAX_REPOS_TO_PROCESS]
        print(
            f"[*] MAX_REPOS_TO_PROCESS={MAX_REPOS_TO_PROCESS} limit is enabled. "
            f"Unprocessed repositories to process: {len(gh_repos)} "
            f"out of {remaining_repos_before_limit}"
        )

    total_repos = len(gh_repos)

    gl_namespace_id = get_gitlab_namespace_id()

    stats = {
        "processed": 0,
        "created_or_existing": 0,
        "created": 0,
        "already_existing": 0,
        "github_empty": 0,
        "github_with_commits": 0,
        "import_finished": 0,
        "files_ok": 0,
        "mirror_ok": 0,
        "github_actions_sync_ok": 0,
        "github_actions_sync_failed": 0,
        "file_transfer_failed": 0,
        "mirror_failed": 0,
        "failed": 0,
    }

    print("\n[*] Starting migration and synchronization setup...\n")

    for index, repo in enumerate(gh_repos, start=1):
        repo_full_name = repo["full_name"]

        stats["processed"] += 1

        print("-" * 80)
        print(f"[{index}/{total_repos}] Repository: {repo_full_name}")

        repo_has_problem = False

        github_has_commits = check_github_repository_has_commits(repo)

        if github_has_commits:
            stats["github_with_commits"] += 1
        else:
            stats["github_empty"] += 1

        project, was_created = create_gitlab_project_from_github(repo, gl_namespace_id)

        if not project:
            print("    └─ [SKIPPED] GitLab project was not created.")
            stats["failed"] += 1
            continue

        print_gitlab_project_permissions(project)

        stats["created_or_existing"] += 1

        if was_created:
            stats["created"] += 1
        else:
            stats["already_existing"] += 1

        mirror_ok = configure_pull_mirror(project, repo)

        print_gitlab_mirror_state(project["id"])

        if mirror_ok:
            stats["mirror_ok"] += 1
            trigger_pull_mirror(project)
        else:
            stats["mirror_failed"] += 1
            print("    ├─ [INFO] GitLab pull mirror is unavailable; GitHub Actions sync will be used")

        import_status = wait_for_import(project["id"])

        if import_status == "finished":
            stats["import_finished"] += 1

        if import_status in ("failed", "timeout", "unknown_error"):
            repo_has_problem = True

        if github_has_commits:
            files_ok = wait_for_gitlab_files(project)
        else:
            print("    ├─ [INFO] The source GitHub repository is empty")
            print("    │  An empty GitLab repository is considered a normal result in this case.")
            files_ok = True

        if files_ok:
            stats["files_ok"] += 1
        else:
            stats["file_transfer_failed"] += 1
            repo_has_problem = True

        github_actions_sync_ok = setup_github_actions_sync(repo)

        if github_actions_sync_ok:
            stats["github_actions_sync_ok"] += 1
        else:
            stats["github_actions_sync_failed"] += 1
            repo_has_problem = True

        if repo_has_problem:
            stats["failed"] += 1

        print_project_summary(
            repo=repo,
            project=project,
            import_status=import_status,
            files_ok=files_ok,
            mirror_ok=mirror_ok,
            github_actions_sync_ok=github_actions_sync_ok,
            github_has_commits=github_has_commits,
            was_created=was_created,
        )

        if not repo_has_problem:
            mark_repo_successfully_processed(
                repo=repo,
                project=project,
                import_status=import_status,
                files_ok=files_ok,
                github_actions_sync_ok=github_actions_sync_ok,
                success_log=success_log,
            )
        else:
            print("    ├─ [INFO] Repository was NOT written to success log because there are problems")

        print("    └─ [DONE] Repository processing completed")

    print("\n" + "=" * 80)
    print(" FINAL REPORT ")
    print("=" * 80)
    print(f"Total processed: {stats['processed']}")
    print(f"GitHub repositories with commits: {stats['github_with_commits']}")
    print(f"Empty GitHub repositories: {stats['github_empty']}")
    print(f"Projects created or found in GitLab: {stats['created_or_existing']}")
    print(f"New GitLab projects created: {stats['created']}")
    print(f"GitLab projects already existed: {stats['already_existing']}")
    print(f"Imports explicitly completed with finished status: {stats['import_finished']}")
    print(f"Files detected or correctly absent due to empty GitHub: {stats['files_ok']}")
    print(f"GitLab pull mirror configured: {stats['mirror_ok']}")
    print(f"GitHub Actions sync GitHub -> GitLab configured: {stats['github_actions_sync_ok']}")
    print(f"File transfer errors: {stats['file_transfer_failed']}")
    print(f"GitLab pull mirror configuration errors: {stats['mirror_failed']}")
    print(f"GitHub Actions sync configuration errors: {stats['github_actions_sync_failed']}")
    print(f"Problematic repositories total: {stats['failed']}")

    if stats["failed"] > 0:
        print("\n[!] Some repositories require manual verification.")
        print("[!] Pay special attention to the following lines:")
        print("    - 'Files in GitLab: NOT COPIED/NOT DETECTED'")
        print("    - 'GitHub Actions sync GitHub -> GitLab: NOT CONFIGURED'")
        print("    - 'Import status: failed'")
        print("    - 'Import status: timeout'")
        print("    - GitHub HTTP responses while creating Actions secret/workflow")
        print("    - GitLab HTTP responses during project import")

    print("=" * 80)


if __name__ == "__main__":
    main()
