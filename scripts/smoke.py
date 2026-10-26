"""Run inside the deployed backend; never print credentials or raw model output."""

import argparse
import json
import os
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(path, *, token="", body=None, key=""):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(
            Request(f"http://127.0.0.1:8000{path}", data=data, headers=headers),
            timeout=100,
        ) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Smoke request {path} failed with HTTP {exc.code}") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Smoke transport failed: {type(exc).__name__}") from None


def validate_evidence(incident, events):
    if incident["status"] != "closed" or incident["provider"] != "copilot":
        raise RuntimeError(
            f"Live incident {incident['id']} did not close through Copilot "
            f"(status={incident['status']}, provider={incident['provider']})."
        )
    required = {"triage", "adk", "observe", "decision", "execute", "verify", "close", "learn"}
    if not required.issubset({event["stage"] for event in events}):
        raise RuntimeError("Live audit is missing required governance stages.")
    reasons = [event["payload"] for event in events if event["stage"] == "reason"]
    if not reasons or any(
        item["provider"] != "copilot" or item["model"] != incident["model"]
        for item in reasons
    ):
        raise RuntimeError("Live inference metadata does not match the configured provider/model.")
    recovery = next(event["payload"] for event in events if event["stage"] == "close")
    if recovery["healthy"] is not True or len(recovery["samples"]) != 3:
        raise RuntimeError("Closure requires three successful independent recovery samples.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--kubernetes", action="store_true")
    parser.add_argument("--github", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("OPERATOR_TOKEN", "")
    if not token:
        raise RuntimeError("Application operator credential is not configured.")
    ready = request("/readyz")
    config = request("/api/config", token=token)
    if (
        ready.get("status") != "ready"
        or ready.get("provider") != "copilot"
        or config.get("provider") != "copilot"
        or config.get("orchestrator") != "google-adk"
        or config.get("simulation_only") is not (not args.kubernetes)
        or config.get("copilot_tool_access") != "disabled"
    ):
        raise RuntimeError("Deployment does not satisfy the live, isolated ADK pilot contract.")
    if args.kubernetes:
        kubernetes = config.get("kubernetes", {})
        if (
            kubernetes.get("enabled") is not True
            or kubernetes.get("owner_approval_required") is not True
            or not kubernetes.get("namespace", "").startswith("remedi-")
        ):
            raise RuntimeError("Kubernetes deployment must retain namespace and owner gates.")
    if args.github:
        repository = config.get("github_gitops", {})
        if (
            not args.kubernetes or repository.get("enabled") is not True
            or repository.get("approval_action") != "create_pull_request"
            or repository.get("manual_merge_required") is not True
        ):
            raise RuntimeError("GitHub mode must retain PR, manual merge and Kubernetes gates.")
    result = {"ready": True, "provider": "copilot", "model": config["model"]}
    if args.live:
        created = request(
            "/api/incidents",
            token=token,
            body={"scenario": "cache_pressure"},
            key=f"deployment-smoke-{uuid.uuid4()}",
        )
        incident_id = created["incident"]["id"]
        deadline = time.monotonic() + 360
        while True:
            incident = request(f"/api/incidents/{incident_id}", token=token)
            if incident["status"] in {
                "closed", "failed", "escalated", "rolled_back", "rejected", "awaiting_approval"
            }:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Live incident {incident_id} timed out.")
            time.sleep(3)
        events = request(f"/api/incidents/{incident_id}/events", token=token)
        validate_evidence(incident, events)
        result.update(live_incident=incident_id, outcome=incident["status"], samples=3)
    else:
        result["live_inference"] = "not requested"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
