"""Refuse occupied ports and reuse an already running workspace instance."""

import argparse
import http.client
import json
import socket

import uvicorn

from .identity import WORKSPACE_ID


def probe_port(port: int) -> str:
    with socket.socket() as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            return "free"
        except OSError:
            pass
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        client.request("GET", "/api/health")
        response = client.getresponse()
        if response.status == 200:
            data = json.loads(response.read(4096))
            if data.get("application") == "tripweave" and data.get("workspace_id") == WORKSPACE_ID:
                return "same"
    except OSError, ValueError, http.client.HTTPException:
        pass
    finally:
        client.close()
    return "occupied"


def main() -> int:
    parser = argparse.ArgumentParser(description="TripWeave local launcher")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    status = probe_port(args.port)
    url = f"http://127.0.0.1:{args.port}"
    if status == "same":
        print(f"TripWeave already running: {url}; no duplicate process started.")
        return 0
    if status == "occupied":
        print(f"Port {args.port} is occupied by an unverified service. No process stopped.")
        print("Choose another free port, for example: .\\start.ps1 -Port 8766")
        return 1
    if args.check_only:
        print(f"Port {args.port} is available.")
        return 0
    # Uvicorn's bind remains authoritative if another process races this preflight.
    uvicorn.run("tripweave.app:app", host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
