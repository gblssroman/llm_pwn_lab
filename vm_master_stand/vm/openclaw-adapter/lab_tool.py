import argparse
import json
import os
import urllib.error
import urllib.request


def main():
    """Read the staging workspace or apply an action with explicit domain fields."""
    parser = argparse.ArgumentParser(description="Local inventory operations workspace")
    parser.add_argument("action", choices=["workspace", "publish-report", "subscribe",
                                          "send-message", "reserve-parts", "update-route"])
    parser.add_argument("--data", type=json.loads, help="JSON object with action fields")
    args = parser.parse_args()
    if args.action != "workspace" and not isinstance(args.data, dict):
        parser.error("--data must be a JSON object for this action")
    request = urllib.request.Request(
        os.environ.get("TOOL_API_URL", "http://tool-api:8080") + "/" + args.action,
        data=None if args.action == "workspace" else json.dumps(args.data).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if args.action == "workspace" else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
