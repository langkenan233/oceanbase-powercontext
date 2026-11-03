"""Create and inspect a PowerContext scope for the Dify demo (stdlib only)."""

import json
import os
import sys
import uuid
from urllib.request import Request, urlopen


BASE = os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")


def post(path, payload):
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    request = Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"init", "verify", "recall"}:
        raise SystemExit("用法: python powercontext_check.py init|verify|recall [scope_id]")
    command = sys.argv[1]
    if command == "init":
        result = post("/v1/scopes", {
            "title": "恶作剧记忆 NPC 演示",
            "summary": "Dify 游戏互动记忆",
            "idempotency_key": "prank-npc-" + uuid.uuid4().hex,
        })
        print("scope_id:", result["scope_id"])
        return
    if len(sys.argv) != 3:
        raise SystemExit("verify/recall 需要 scope_id")
    scope_id = sys.argv[2]
    if command == "verify":
        result = post("/v1/memory/remember", {
            "scope_id": scope_id,
            "kind": "fact",
            "text": "npc_prank 验证记录：玩家曾把月亮藏进茶杯。",
            "reason": "演示记忆读写",
        })
        print("写入记录数:", len(result.get("records", [])))
    result = post("/v1/context/prepare", {
        "scope_id": scope_id,
        "query": "npc_prank",
        "max_bytes": 1600,
    })
    print("召回状态:", result.get("status"))
    print("召回内容:", result.get("content"))
    if command == "verify" and "npc_prank" not in (result.get("content") or ""):
        raise SystemExit("验证失败：未召回刚写入的记忆")


if __name__ == "__main__":
    main()
