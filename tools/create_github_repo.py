from __future__ import annotations

import argparse
import os

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_name")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set GITHUB_TOKEN first.")

    response = requests.post(
        "https://api.github.com/user/repos",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "name": args.repo_name,
            "private": args.private,
            "auto_init": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    print(payload.get("html_url"))
    print(payload.get("clone_url"))


if __name__ == "__main__":
    main()
