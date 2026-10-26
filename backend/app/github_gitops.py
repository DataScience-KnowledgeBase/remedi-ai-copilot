import base64
import difflib
import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import Settings
from app.domain import KUBE_TARGETS
from app.providers import ProviderError

MANIFEST_PATH = "kubernetes/examples.yaml"
CI_CHECK = "Lint, tests, audits and browser workflow"
ACTIONS_APP_ID = 15368


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def documents(content: str) -> list[dict]:
    try:
        items = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        raise ProviderError("Repository Kubernetes manifest is invalid YAML") from exc
    if (
        len(items) != 4
        or any(not isinstance(item, dict) for item in items)
        or any(not isinstance(item.get("metadata"), dict) for item in items)
        or {item.get("metadata", {}).get("name") for item in items} != set(KUBE_TARGETS.values())
        or any(
            item.get("kind") != "Deployment" or item.get("apiVersion") != "apps/v1"
            for item in items
        )
    ):
        raise ProviderError(
            "Repository manifest must contain exactly the four approved Deployments"
        )
    return items


def target_document(content: str, scenario: str) -> dict:
    return next(
        item for item in documents(content) if item["metadata"]["name"] == KUBE_TARGETS[scenario]
    )


def field_parent(document: dict, pointer: str) -> tuple[dict | list, str | int]:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    parent = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    return parent, key


def manifest_change(content: str, scenario: str, change: dict) -> str:
    items = documents(content)
    updated = deepcopy(items)
    target = next(item for item in updated if item["metadata"]["name"] == KUBE_TARGETS[scenario])
    try:
        parent, key = field_parent(target, change["path"])
        if parent[key] != change["before"]:
            raise ProviderError("Repository configuration differs from the live approved baseline")
        if change["operation"] == "remove":
            del parent[key]
        else:
            parent[key] = change["after"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderError(
            "Approved field is absent or incompatible in repository configuration"
        ) from exc
    return yaml.safe_dump_all(updated, sort_keys=False, allow_unicode=False)


class GitHubGitOps:
    """Installation-scoped API client. No merge endpoint or model-supplied paths."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo = settings.github_repository
        self.root = f"/repos/{self.repo}"
        self._token = ""
        self._renew_at = 0.0

    def app_jwt(self) -> str:
        try:
            key = serialization.load_pem_private_key(
                Path(self.settings.github_private_key_file).read_bytes(),
                password=None,
            )
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ProviderError("GitHub App key must be an RSA private key")
            stamp = int(time.time())

            def encoded(value: dict) -> bytes:
                return base64.urlsafe_b64encode(
                    json.dumps(value, separators=(",", ":")).encode(),
                ).rstrip(b"=")

            message = (
                encoded({"alg": "RS256", "typ": "JWT"})
                + b"."
                + encoded(
                    {
                        "iat": stamp - 60,
                        "exp": stamp + 540,
                        "iss": str(self.settings.github_app_id),
                    }
                )
            )
            signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            return (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
        except (OSError, ValueError, TypeError) as exc:
            raise ProviderError("GitHub App private key is unavailable or invalid") from exc

    async def api(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        jwt: str | None = None,
        missing: bool = False,
    ) -> dict | list | None:
        if not self.settings.github_gitops_enabled:
            raise ProviderError("GitHub repository-backed remediation is disabled")
        if jwt is None and (not self._token or time.monotonic() >= self._renew_at):
            installation = await self.api(
                "POST",
                f"/app/installations/{self.settings.github_installation_id}/access_tokens",
                {
                    "repositories": [self.repo.split("/")[1]],
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "actions": "read",
                        "checks": "read",
                    },
                },
                jwt=self.app_jwt(),
            )
            self._token = installation["token"]
            self._renew_at = time.monotonic() + 3000
        try:
            async with httpx.AsyncClient(
                timeout=20, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    "https://api.github.com" + path,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {jwt or self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            if response.status_code == 404 and missing:
                return None
            if not response.is_success:
                raise ProviderError(
                    f"GitHub {method} failed (HTTP {response.status_code}); "
                    "check App installation, repository permissions and branch state."
                )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(
                f"GitHub request failed ({type(exc).__name__}); no fallback"
            ) from exc

    async def main_sha(self) -> str:
        ref = await self.api("GET", self.root + "/git/ref/heads/main")
        return ref["object"]["sha"]

    async def file(self, sha: str) -> dict:
        result = await self.api(
            "GET",
            self.root + f"/contents/{MANIFEST_PATH}?ref={quote(sha, safe='')}",
            missing=True,
        )
        if result is None:
            raise ProviderError(
                "Repository Kubernetes manifests are not published on main. "
                "Merge the reviewed integration/setup PR first; no local-file fallback."
            )
        if result.get("encoding") != "base64" or result.get("size", 0) > 100_000:
            raise ProviderError("Repository manifest is not a bounded UTF-8 text file")
        try:
            content = base64.b64decode(result["content"]).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("Repository manifest cannot be decoded") from exc
        documents(content)
        return {"content": content, "blob_sha": result["sha"], "commit_sha": sha}

    async def snapshot(self) -> dict:
        return await self.file(await self.main_sha())

    async def prepare(self, scenario: str, change: dict, snapshot: dict) -> dict:
        if await self.main_sha() != snapshot["commit_sha"]:
            raise ProviderError(
                "Repository main changed during diagnosis; analyse a fresh incident"
            )
        before = snapshot["content"]
        after = manifest_change(before, scenario, change)
        return {
            "repository": self.repo,
            "base_branch": "main",
            "base_sha": snapshot["commit_sha"],
            "path": MANIFEST_PATH,
            "blob_sha": snapshot["blob_sha"],
            "before_content": before,
            "after_content": after,
            "after_digest": content_digest(after),
            "change": change,
            "diff": "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile=f"a/{MANIFEST_PATH}",
                    tofile=f"b/{MANIFEST_PATH}",
                )
            ),
        }

    def validate_checkpoint(self, plan: dict, scenario: str):
        if (
            plan["repository"] != self.repo
            or plan["path"] != MANIFEST_PATH
            or plan["base_branch"] != "main"
            or content_digest(plan["after_content"]) != plan["after_digest"]
            or manifest_change(plan["before_content"], scenario, plan["change"])
            != plan["after_content"]
        ):
            raise ProviderError("Repository checkpoint is outside the approved exact diff")

    async def open_pr(self, incident_id: str, scenario: str, plan: dict) -> dict:
        self.validate_checkpoint(plan, scenario)
        branch = f"remedi/incident-{incident_id}"
        ref_path = self.root + f"/git/ref/heads/{branch}"
        ref = await self.api("GET", ref_path, missing=True)
        if ref is None:
            if await self.main_sha() != plan["base_sha"]:
                raise ProviderError(
                    "Repository main changed after approval; reanalyse before writing"
                )
            await self.api(
                "POST",
                self.root + "/git/refs",
                {
                    "ref": f"refs/heads/{branch}",
                    "sha": plan["base_sha"],
                },
            )
            head_sha = plan["base_sha"]
        else:
            head_sha = ref["object"]["sha"]
        if head_sha == plan["base_sha"]:
            written = await self.api(
                "PUT",
                self.root + f"/contents/{MANIFEST_PATH}",
                {
                    "message": (
                        f"fix: owner-approved remediation for incident {incident_id}\n\n"
                        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
                    ),
                    "content": base64.b64encode(plan["after_content"].encode()).decode(),
                    "sha": plan["blob_sha"],
                    "branch": branch,
                },
            )
            head_sha = written["commit"]["sha"]
        await self.validate_head(head_sha, plan)
        matches = await self.api(
            "GET",
            self.root
            + "/pulls?state=all&base=main&head="
            + quote(self.repo.split("/")[0] + ":" + branch, safe="")
            + "&per_page=100",
        )
        if len(matches) > 1:
            raise ProviderError(
                "Multiple PRs exist for this incident branch; operator review required"
            )
        pr = (
            matches[0]
            if matches
            else await self.api(
                "POST",
                self.root + "/pulls",
                {
                    "title": f"Remediate {KUBE_TARGETS[scenario]} ({incident_id})",
                    "head": branch,
                    "base": "main",
                    "body": (
                        f"Owner-approved incident `{incident_id}`.\n\n"
                        f"Only `{MANIFEST_PATH}` may change. Diff reviewed in the application.\n"
                        "Human merge and successful merge-commit CI are required for deployment.\n"
                        "The application will not merge this PR or bypass branch protection."
                    ),
                },
            )
        )
        if pr["head"]["sha"] != head_sha or pr["base"]["ref"] != "main":
            raise ProviderError("Incident PR does not match the approved branch")
        return {"number": pr["number"], "url": pr["html_url"], "head_sha": head_sha}

    async def validate_head(self, sha: str, plan: dict):
        file = await self.file(sha)
        compare = await self.api("GET", self.root + f"/compare/{plan['base_sha']}...{sha}")
        files = compare.get("files", [])
        if (
            file["content"] != plan["after_content"]
            or compare.get("ahead_by") != 1
            or len(files) != 1
            or files[0]["filename"] != MANIFEST_PATH
            or files[0].get("status") != "modified"
        ):
            raise ProviderError(
                "PR branch contains changes outside the exact approved manifest diff"
            )

    async def merged(self, pr: dict, plan: dict) -> str | None:
        result = await self.api("GET", self.root + f"/pulls/{pr['number']}")
        if (
            result["head"]["sha"] != pr["head_sha"]
            or result["base"]["ref"] != "main"
            or result["base"]["repo"]["full_name"] != self.repo
            or result["head"]["repo"]["full_name"] != self.repo
        ):
            raise ProviderError("PR was modified after owner approval; no deployment permitted")
        if not result.get("merged"):
            if result["state"] == "closed":
                raise ProviderError("Remediation PR was closed without merge; no deployment")
            return None
        if result.get("merged_by", {}).get("type") != "User":
            raise ProviderError("A human must merge the remediation PR")
        files = await self.api("GET", self.root + f"/pulls/{pr['number']}/files?per_page=100")
        if len(files) != 1 or files[0]["filename"] != MANIFEST_PATH:
            raise ProviderError("Merged PR contains unexpected file changes")
        sha = result["merge_commit_sha"]
        if (await self.file(sha))["content"] != plan["after_content"]:
            raise ProviderError("Merged manifest no longer matches the owner-approved diff")
        return sha

    async def ci_passed(self, sha: str) -> bool:
        result = await self.api("GET", self.root + f"/commits/{sha}/check-runs?per_page=100")
        if result["total_count"] > 100:
            raise ProviderError("Too many check runs to verify the merge-commit gate")
        checks = [
            item
            for item in result["check_runs"]
            if item["name"] == CI_CHECK and item["app"]["id"] == ACTIONS_APP_ID
        ]
        if not checks:
            return False
        latest = max(checks, key=lambda item: item["id"])
        if latest["status"] != "completed":
            return False
        if latest["conclusion"] != "success":
            raise ProviderError("Merged remediation CI did not succeed; no cluster change applied")
        return True
