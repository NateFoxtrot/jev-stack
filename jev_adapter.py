#!/usr/bin/env python3
"""JEV decision-protocol adapter. Exposes POST /v1/decision backed by omniroute chat glm/glm-5.3-flash."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_URL = "http://127.0.0.1:20128/v1/chat/completions"
import os
MODEL = os.environ.get("OMNI_MODEL", "glm/glm-5.3-flash")
BACKEND_TIMEOUT = 120
AUTH_TOKEN = os.environ.get("OMNIROUTE_API_KEY", "")

SYSTEM_PROMPT = (
    "You are strict decision scorer. For each numbered CONTEXT and each schema field, "
    "output field value and your confidence 0.00-1.00. Output ONLY a JSON array, one "
    "object per context, same order; each object: "
    ' Pe{"fields": {<field>: {"value": <val>, "probability": <float>}}}.'
)

def log_line(n_contexts, ms, status):
    ts = datetime.now(timezone.utc).isoformat()
    sys.stderr.write(f"{ts} contexts={n_contexts} ms={ms:.1f} status={status}\n")
    sys.stderr.flush()

def clamp_prob(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.0
    if p != p or p in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, p))

def call_backend(user_prompt):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 16000,
        "temperature": 0,
    }
    req = urllib.request.Request(
        BACKEND_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if AUTH_TOKEN:
        req.add_header("Authorization", "Bearer " + AUTH_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=BACKEND_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise ValueError(f"backend HTTP {exc.code}: {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise ValueError(f"backend unreachable: {exc.reason}")
    except Exception as exc:
        raise ValueError(f"backend call failed: {exc}")
    try:
        parsed = json.loads(body)
        return parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed backend response: {exc}")

def extract_json_array(text):
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    raw = fence.group(1) if fence else text[text.find("["): text.rfind("]") + 1]
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("model output is not a JSON array")
    return data

def extract_fields_order(schema):
    if isinstance(schema, list):
        return {s["name"]: {k: v for k, v in s.items() if k != "name"} for s in schema if isinstance(s, dict) and "name" in s}
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            return dict(props)
        return {k: v for k, v in schema.items() if k != "type"}
    return {}

def build_prompt(instructions, schema, contexts, fields_order):
    field_lines = "\n".join('- "{}": {}'.format(n, json.dumps(s)) for n, s in fields_order.items())
    numbered = "\n".join("{}. {}".format(i + 1, c) for i, c in enumerate(contexts))
    return (
        "{}\n\nSchema fields:\n{}\n\n"
        "For each numbered context, return ONLY a JSON array. Each element must be exactly: "
        '{{"fields": {{"<field>": {{"value": <value>, "probability": <0.0-1.0>}}}}}}. '
        "Values must conform to the schema; use null when unknown. "
        "No prose, no markdown fences.\n\nContexts:\n{}"
    ).format(instructions, field_lines, numbered)

def build_response(parsed_list, fields_order):
    results = []
    for entry in parsed_list:
        entry_fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
        if not isinstance(entry_fields, dict):
            entry_fields = {}
        decision, fields = {}, {}
        for name, spec in fields_order.items():
            info = entry_fields.get(name)
            if not isinstance(info, dict):
                info = {}
            value = info.get("value")
            try:
                prob = float(info.get("probability", 0.0))
            except (TypeError, ValueError):
                prob = 0.0
            prob = min(1.0, max(0.0, prob))
            enums = spec.get("enum") if isinstance(spec, dict) else None
            if enums and value not in enums:
                value, prob = None, 0.0
            decision[name] = value
            fields[name] = {"value": value, "probability": prob}
        results.append({"decision": decision, "fields": fields})
    return {"object": "decision", "results": results, "usage": {"backend": MODEL}}

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/decision":
            self._send(404, {"error": {"message": "not found"}})
            return
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            instructions = payload.get("instructions")
            schema = payload.get("schema")
            contexts = payload.get("contexts")
            if not isinstance(instructions, str) or not instructions.strip():
                raise ValueError("instructions must be a non-empty string")
            if not isinstance(schema, (dict, list)):
                raise ValueError("schema must be an object or array")
            if not isinstance(contexts, list) or not contexts or not all(isinstance(c, str) for c in contexts):
                raise ValueError("contexts must be a non-empty list of strings")
            fields_order = extract_fields_order(schema)
            prompt = build_prompt(instructions, schema, contexts, fields_order)
            content = call_backend(prompt)
            parsed = extract_json_array(content)
            response = build_response(parsed, fields_order)
            ms = (time.time() - t0) * 1000.0
            print("{} contexts={} ms={:.1f}".format(time.strftime("%Y-%m-%dT%H:%M:%S"), len(contexts), ms), file=sys.stderr)
            self._send(200, response)
        except Exception as e:
            print("{} error ms={:.1f} {}".format(time.strftime("%Y-%m-%dT%H:%M:%S"), (time.time() - t0) * 1000.0, e), file=sys.stderr)
            self._send(502, {"error": {"message": str(e)}})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8496)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()

if __name__ == "__main__":
    main()
