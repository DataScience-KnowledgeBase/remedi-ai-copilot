"""Report supported SDK authentication status without printing credentials."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.copilot_provider import CopilotInference
from app.providers import Provider, ProviderError, completion_metadata


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make ONE synthetic inference; consumes account entitlement",
    )
    args = parser.parse_args()
    settings = Settings()
    if settings.provider != "copilot":
        parser.error("The live probe requires PROVIDER=copilot; fixture output is not inference.")
    inference = CopilotInference(settings)
    try:
        client = await inference._connect()
        auth = await client.get_auth_status()
        result = {
            "authenticated": auth.isAuthenticated,
            "sdk_version": "1.0.25",
            "configured_model": settings.copilot_model,
        }
        if auth.isAuthenticated:
            result["available_models"] = [model.id for model in await client.list_models()]
        else:
            result["next_step"] = "Run scripts\\copilot-login.ps1 locally; no tokens in chat."
        print(json.dumps(result, indent=2))
    finally:
        await inference.close()
    if args.live:
        provider = Provider(settings)
        try:
            completion = await provider.complete(
                {
                    "synthetic": True,
                    "service": "checkout",
                    "observations": [],
                    "step": 1,
                }
            )
            report = {
                "status": "completed",
                "synthetic_only": True,
                "step": completion.step.model_dump(),
                "telemetry": completion_metadata(completion, provider),
            }
        except ProviderError as exc:
            report = {"status": "blocked", "reason": str(exc), "synthetic_only": True}
        finally:
            await provider.close()
        output = Path("reports/copilot-live-probe.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        if report["status"] != "completed":
            raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
