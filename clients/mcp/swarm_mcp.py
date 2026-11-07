#!/usr/bin/env python3
"""Tiny stdio MCP server exposing swarm_remember + swarm_recall. Stdlib only.

Claude Code:
  claude mcp add swarm-memory -e SWARM_URL=https://<instance> -e SWARM_SECRET=... \
      -e SWARM_USER=alice -- python3 /path/to/clients/mcp/swarm_mcp.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from swarm_client import Swarm  # noqa: E402

TOOLS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools.json")))
MCP_TOOLS = [{"name": t["function"]["name"], "description": t["function"]["description"],
              "inputSchema": t["function"]["parameters"]} for t in TOOLS]

swarm = None


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, id_ = req.get("method"), req.get("id")
    if id_ is None:  # notification (e.g. notifications/initialized)
        continue
    if method == "initialize":
        reply(id_, {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hivemind", "version": "0.2.0"}})
    elif method == "tools/list":
        reply(id_, {"tools": MCP_TOOLS})
    elif method == "tools/call":
        p = req.get("params", {})
        try:
            swarm = swarm or Swarm()
            out = swarm.dispatch(p.get("name"), p.get("arguments") or {})
            reply(id_, {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False})
        except Exception as e:  # surface as tool error, keep server alive
            reply(id_, {"content": [{"type": "text", "text": f"swarm-memory error: {e}"}], "isError": True})
    elif method == "ping":
        reply(id_, {})
    else:
        reply(id_, error={"code": -32601, "message": f"method not found: {method}"})
