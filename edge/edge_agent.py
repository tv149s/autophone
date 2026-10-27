#!/usr/bin/env python3
"""边缘端桥接 —— 连接公网控制面(autophoneagent),收任务转给本地 duplex,回传结果。

不改 duplex:纯文件桥接。
- 连 wss://ws.autophoneagent.com/ws/edge?token=<设备令牌>
- 收到任务 → 写入 ~/tools/voice/tasks/pending/(duplex 空闲时自动拨号)
- 监视 ~/tools/voice/tasks/done/,带 server_task_id 的完成文件 → 回传结果
- 30 秒心跳保活(穿 Cloudflare),断线自动重连

令牌:环境变量 EDGE_TOKEN,或 ~/tools/voice/edge_token.txt
用法:venv312/bin/python -u edge_agent.py   (日志 edge_agent.log)
"""
import asyncio
import glob
import http.server
import json
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = f"{HERE}/tasks"
WS_URL = os.environ.get("EDGE_WS", "wss://ws.autophoneagent.com/ws/edge")
API = os.environ.get("EDGE_API", "https://api.autophoneagent.com")
GOOGLE_CLIENT_ID = os.environ.get(
    "EDGE_GOOGLE_CLIENT_ID",
    "1011075408383-9jpp7rta3o6uo92b8dph5bm7it4st6vu.apps.googleusercontent.com",
)
LOGIN_PORT = int(os.environ.get("EDGE_LOGIN_PORT", "8799"))
TOKEN_FILE = f"{HERE}/edge_token.txt"
REPORTED = f"{HERE}/.edge_reported.json"
UA = {"User-Agent": "Mozilla/5.0 AutoPhoneAgent-Edge"}   # Cloudflare 会拦默认 urllib UA


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def api_post(path, data, token=None):
    headers = {"Content-Type": "application/json", **UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, json.dumps(data).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


LOGIN_HTML = """<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<title>AutoPhoneAgent 设备登录</title>
<script src="https://accounts.google.com/gsi/client" async></script>
<style>body{{font:16px system-ui;display:flex;flex-direction:column;align-items:center;gap:20px;padding:60px}}</style>
</head><body>
<h2>用 Google 登录以把这台设备接入你的账号</h2>
<div id="g_id_onload" data-client_id="{cid}" data-callback="onCred" data-auto_prompt="false"></div>
<div class="g_id_signin" data-type="standard" data-size="large" data-text="continue_with"></div>
<p id="msg"></p>
<script>
function onCred(resp){{
  document.getElementById('msg').textContent='登录中…';
  fetch('/credential',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{credential:resp.credential}})}})
    .then(r=>r.ok?document.body.innerHTML='<h2>✅ 设备已接入,可关闭此页回到终端。</h2>'
                 :r.text().then(t=>document.getElementById('msg').textContent='失败:'+t));
}}
</script></body></html>"""


def interactive_login():
    """起本地登录页 → 浏览器 Google 登录 → 换 JWT → 自助注册设备 → 返回设备令牌。"""
    result = {}
    done = threading.Event()
    host = socket.gethostname()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(LOGIN_HTML.format(cid=GOOGLE_CLIENT_ID).encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                jwt = api_post("/auth/google", {"credential": body["credential"]})["access_token"]
                dev = api_post("/devices/register",
                               {"name": host, "kind": "mac", "hostname": host}, token=jwt)
                result["token"] = dev["token"]
                result["name"] = dev.get("name")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            done.set()

    srv = http.server.HTTPServer(("127.0.0.1", LOGIN_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{LOGIN_PORT}"
    log(f"需要登录接入设备。正在打开浏览器:{url}(没弹出就手动访问)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    if not done.wait(timeout=300):
        srv.shutdown()
        raise SystemExit("登录超时,请重试")
    srv.shutdown()
    tok = result.get("token")
    if not tok:
        raise SystemExit("登录/注册失败,请重试")
    open(TOKEN_FILE, "w").write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    log(f"设备已接入:{result.get('name')},令牌已保存")
    return tok


def get_token():
    t = os.environ.get("EDGE_TOKEN")
    if t:
        return t.strip()
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return interactive_login()


def load_reported() -> set:
    try:
        return set(json.load(open(REPORTED)))
    except Exception:
        return set()


def save_reported(s: set):
    json.dump(list(s), open(REPORTED, "w"))


def write_pending(task: dict) -> str:
    """把服务器任务写成 duplex 兼容的 pending 文件,嵌入 server_task_id 以便回报。"""
    card = task.get("card", {}) or {}
    sid = task.get("id", "")
    fn = f"{time.strftime('%Y%m%d_%H%M%S')}_srv_{sid[:8]}.json"
    doc = {
        "to": card.get("to", ""),
        "to_name": card.get("to_name", ""),
        "goal": card.get("goal", ""),
        "may_share": card.get("may_share", []),
        "may_decide": card.get("may_decide", ""),
        "must_not": card.get("must_not", "Do not give payment card numbers or agree to payments."),
        "language": card.get("language", ""),
        "source": "web",
        "server_task_id": sid,
        "id": fn[:-5],
    }
    os.makedirs(f"{TASKS}/pending", exist_ok=True)
    json.dump(doc, open(f"{TASKS}/pending/{fn}", "w"), ensure_ascii=False, indent=2)
    return fn


async def watch_done(ws):
    """监视 done/ 里新完成且带 server_task_id 的任务,回报服务器。"""
    reported = load_reported()
    while True:
        for f in sorted(glob.glob(f"{TASKS}/done/*.json")):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            sid = d.get("server_task_id")
            if not sid or sid in reported:
                continue
            outcome = d.get("outcome", {}) or {}
            st = outcome.get("status")
            status = "failed" if st in ("failed", "no_answer", "error") else "done"
            await ws.send(json.dumps({"type": "report", "task_id": sid, "status": status, "result": outcome}))
            reported.add(sid)
            save_reported(reported)
            log(f"回报结果 → 服务器任务 {sid}: {status} / {outcome.get('summary_zh') or outcome.get('status')}")
        await asyncio.sleep(2)


async def run(token):
    url = f"{WS_URL}?token={token}"
    async with websockets.connect(url, open_timeout=20, ping_interval=30, ping_timeout=20) as ws:
        hello = json.loads(await ws.recv())
        log("已连上控制面:", hello)
        watcher = asyncio.create_task(watch_done(ws))
        try:
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "task":
                    t = msg["task"]
                    fn = write_pending(t)
                    log(f"收到任务 {t.get('id','')[:8]} → 写入 pending/{fn}")
                    await ws.send(json.dumps({"type": "report", "task_id": t["id"], "status": "running"}))
                elif msg.get("type") == "hello":
                    pass
        finally:
            watcher.cancel()


async def main():
    token = get_token()
    while True:
        try:
            await run(token)
        except Exception as e:
            log("连接断开,5 秒后重连:", type(e).__name__, str(e)[:120])
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
