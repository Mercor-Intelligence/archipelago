"""Example script to configure MCP servers and populate data via API endpoints."""

import json

import requests

BASE_URL = "http://localhost:8080"


def configure_mcp_servers():
    """Configure MCP servers (docs, filesystem, sheets, slides)."""
    mcp_config = {
        "mcpServers": {
            "pdf_server": {
                "transport": "stdio",
                "command": "python",
                "args": ["main.py"],
                "env": {"PYTHONPATH": ".", "APP_FS_ROOT": "/filesystem"},
                "cwd": "./mcp_servers/pdf_server",
            },
            "docs_server": {
                "transport": "stdio",
                "command": "python",
                "args": ["main.py"],
                "env": {"PYTHONPATH": ".", "APP_FS_ROOT": "/filesystem"},
                "cwd": "./mcp_servers/docs_server",
            },
            "filesystem_server": {
                "transport": "stdio",
                "command": "python",
                "args": ["main.py"],
                "env": {"PYTHONPATH": ".", "APP_FS_ROOT": "/filesystem"},
                "cwd": "./mcp_servers/filesystem_server",
            },
            "sheets_server": {
                "transport": "stdio",
                "command": "python",
                "args": ["main.py"],
                "env": {"PYTHONPATH": ".", "APP_FS_ROOT": "/filesystem"},
                "cwd": "./mcp_servers/sheets_server",
            },
            "slides_server": {
                "transport": "stdio",
                "command": "python",
                "args": ["main.py"],
                "env": {"PYTHONPATH": ".", "APP_FS_ROOT": "/filesystem"},
                "cwd": "./mcp_servers/slides_server",
            },
        }
    }

    url = f"{BASE_URL}/apps"

    response = requests.post(
        url, json=mcp_config, headers={"Content-Type": "application/json"}
    )

    print(f"Status Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")

    if response.status_code == 200:
        result = response.json()
        print(
            f"\n✓ Successfully configured {len(result['servers'])} server(s): {', '.join(result['servers'])}"
        )
        return True
    else:
        print(f"\n✗ Error: {response.text}")
        return False


def populate_from_s3():
    """Example: Populate filesystem subsystem from S3 bucket."""
    populate_config = {
        "sources": [
            {
                "url": "s3://rl-studio-snapshots-prod/worlds/snap_c9f0693abd924a32b480291608a09065/filesystem/",  # IB 201
                "subsystem": "filesystem",
            }
        ]
    }

    url = f"{BASE_URL}/data/populate/s3"

    response = requests.post(
        url, json=populate_config, headers={"Content-Type": "application/json"}
    )

    print(f"\n{'=' * 60}")
    print("Populate from S3 Request")
    print(f"{'=' * 60}")
    print(f"Status Code: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        print(f"Response: {json.dumps(result, indent=2)}")
        print(
            f"\n✓ Successfully populated {result['objects_added']} object(s) into filesystem subsystem"
        )
        return True
    else:
        print(f"\n✗ Error: {response.text}")
        return False


def snapshot_data():
    """Example: Snapshot data and stream tar.gz file."""
    url = f"{BASE_URL}/data/snapshot"
    response = requests.post(url, stream=True)
    print(f"Status Code: {response.status_code}")

    if response.status_code == 200:
        # Get filename from Content-Disposition header
        content_disposition = response.headers.get("Content-Disposition", "")
        filename = "snapshot.tar.gz"
        if "filename=" in content_disposition:
            filename = content_disposition.split("filename=")[1].strip('"')

        # Stream the tar.gz file to disk
        output_path = filename
        total_bytes = 0
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)

        print(
            f"✓ Successfully downloaded snapshot: {output_path} ({total_bytes} bytes)"
        )
        return True
    else:
        print(f"✗ Error: {response.text}")
        return False


if __name__ == "__main__":
    populate_from_s3()
    configure_mcp_servers()
    snapshot_data()
