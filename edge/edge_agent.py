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
import sqlite3
import subprocess
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
LOGIN_PORT = int(os.environ.get("EDGE_LOGIN_PORT", "5173"))
TOKEN_FILE = f"{HERE}/edge_token.txt"
REPORTED = f"{HERE}/.edge_reported.json"
CONTACTS_FLAG = f"{HERE}/.contacts_synced"
CACHE = f"{HERE}/contacts_cache.json"           # duplex 来电匹配读这个
MEM_REPORTED = f"{HERE}/.edge_mem_reported.json"
CALLS = f"{HERE}/calls"
CHANNELS_JSON = f"{HERE}/channels.json"
CTL_STATUS = "http://127.0.0.1:8766/status"
PHONES_JSON = os.path.expanduser("~/tools/phones.json")


def active_phone():
    """当前启用的受控电话(phones.json)。"""
    try:
        p = json.load(open(PHONES_JSON))
        dev = p["active"]
        c = p["phones"][dev]
        return {"pkey": dev, "label": c.get("label", dev), "platform": c.get("platform", "ios"),
                "udid": c.get("udid", ""), "number": c.get("number", "")}
    except Exception:
        return {"pkey": "iphone-13", "label": "iPhone 13", "platform": "ios",
                "udid": "00008110-000E61682EE2401E", "number": "+14255480368"}


WECHAT_BUNDLE = "com.tencent.xin"


def gather_phone_info() -> list:
    """汇总本边缘端控制的 iPhone:锁屏/就绪/前台应用(经 callctl,不直接碰 WDA)+ 渠道能力与动态警告。"""
    locked, wda_ready, front_app = False, False, None
    try:
        r = json.load(urllib.request.urlopen(CTL_STATUS, timeout=4))
        locked = bool(r.get("locked"))
        front_app = r.get("front_app")
        age = r.get("syslog_age_s")
        wda_ready = age is not None and age < 60      # callctl 在实时读 syslog = 控制链路通
    except Exception:
        pass
    channels = []
    try:
        for c in json.load(open(CHANNELS_JSON)).get("channels", []):
            supported = c.get("status") == "supported"
            warn = ""
            if c.get("bundle") == WECHAT_BUNDLE and front_app == WECHAT_BUNDLE:
                warn = "微信正在前台,此时来电不走系统界面,可能漏接(请回到主屏幕)"
            channels.append({
                "kind": c.get("id"), "name": c.get("name"), "status": c.get("status"),
                "enabled": bool(c.get("enabled")),
                "answer": supported,                                   # 可接听
                "outbound": supported and c.get("kind") == "cellular",  # 外呼目前仅蜂窝电话
                "warn": warn,
            })
    except Exception:
        pass
    front_label = {WECHAT_BUNDLE: "微信", "com.apple.springboard": "主屏幕"}.get(front_app, front_app or "")
    ph = active_phone()
    return [{
        "pkey": ph["pkey"], "label": ph["label"], "platform": ph["platform"],
        "udid": ph["udid"], "number": ph["number"],
        "wda_ready": wda_ready, "locked": locked, "front_app": front_label, "channels": channels,
    }]


def phone_registry():
    """本边缘端可切换的电话清单(phones.json),供网页展示+切换。ready=WDA已编译,可切换。"""
    try:
        p = json.load(open(PHONES_JSON))
        out = []
        for k, v in p.get("phones", {}).items():
            ready = v.get("platform", "ios") != "ios" or bool(
                glob.glob(f"/Volumes/min-dev/wda-dd-{k}/Build/Products/*.xctestrun"))
            out.append({"id": k, "label": v.get("label", k), "platform": v.get("platform", "ios"),
                        "active": k == p.get("active"), "ready": ready})
        return out
    except Exception:
        return []


async def send_phone_info(ws):
    try:
        await ws.send(json.dumps({"type": "phone_info", "phones": gather_phone_info(),
                                  "registry": phone_registry()}))
    except Exception:
        pass


def do_switch_phone(pid: str):
    """执行切换(detached:脚本会杀掉本 edge_agent 并在结束时重启,所以必须独立会话)。"""
    sh = os.path.expanduser("~/tools/switch-phone.sh")
    logf = open(os.path.expanduser("~/tools/voice/switch.log"), "a")
    subprocess.Popen(["zsh", sh, pid], stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


async def report_phones(ws):
    """每 5 秒探测一次,状态有变化就立即上报(锁屏/亮屏、前台应用等动态跟随);最多 30 秒心跳一次。"""
    last_sig, since = None, 0
    while True:
        info = gather_phone_info()
        reg = phone_registry()
        sig = json.dumps([info, reg], ensure_ascii=False, sort_keys=True)
        if sig != last_sig or since >= 6:
            try:
                await ws.send(json.dumps({"type": "phone_info", "phones": info, "registry": reg}))
            except Exception:
                return
            last_sig, since = sig, 0
        else:
            since += 1
        await asyncio.sleep(5)


def apply_channel_config(cfg: dict) -> bool:
    """把网页下发的渠道设置写入 channels.json(duplex 会热加载生效)。cfg: {kind:{enabled,asr,reply_language}}"""
    try:
        doc = json.load(open(CHANNELS_JSON))
    except Exception:
        return False
    changed = False
    for ch in doc.get("channels", []):
        o = cfg.get(ch.get("id"))
        if not o:
            continue
        for k in ("enabled", "asr", "reply_language"):
            if o.get(k) is not None and ch.get(k) != o[k]:
                ch[k] = o[k]
                changed = True
    if changed:
        json.dump(doc, open(CHANNELS_JSON, "w"), ensure_ascii=False, indent=2)
    return changed
UA = {"User-Agent": "Mozilla/5.0 AutoPhoneAgent-Edge"}   # Cloudflare 会拦默认 urllib UA


def read_mac_contacts() -> list:
    """读 macOS 通讯录(iPhone 经 iCloud 同步而来)。需 Terminal 有完全磁盘访问权限。"""
    dbs = glob.glob(os.path.expanduser(
        "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"))
    main = os.path.expanduser("~/Library/Application Support/AddressBook/AddressBook-v22.abcddb")
    if os.path.exists(main):
        dbs.append(main)
    seen, out = set(), []
    for d in dbs:
        try:
            con = sqlite3.connect(f"file:{d}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT COALESCE(r.ZFIRSTNAME,''),COALESCE(r.ZLASTNAME,''),"
                "COALESCE(r.ZORGANIZATION,''),p.ZFULLNUMBER "
                "FROM ZABCDRECORD r JOIN ZABCDPHONENUMBER p ON p.ZOWNER=r.Z_PK"
            ).fetchall()
            con.close()
        except Exception:
            continue
        for fn, ln, org, num in rows:
            if not num or num in seen:
                continue
            seen.add(num)
            name = " ".join(x for x in (fn, ln) if x).strip()
            out.append({"name": name, "phone": num, "org": org or "", "tags": []})
    return out


async def maybe_sync_contacts(ws):
    """首次连上时把本机通讯录上报一次(服务器按号码去重合并)。EDGE_SYNC_CONTACTS=1 可强制重传。"""
    if os.path.exists(CONTACTS_FLAG) and not os.environ.get("EDGE_SYNC_CONTACTS"):
        return
    try:
        contacts = read_mac_contacts()
    except Exception as e:
        log("读取本机通讯录失败:", e)
        return
    if not contacts:
        log("本机通讯录为空或读不到(检查完全磁盘访问权限)")
        return
    await ws.send(json.dumps({"type": "contacts_import", "contacts": contacts}))
    log(f"已上报本机通讯录 {len(contacts)} 条")
    open(CONTACTS_FLAG, "w").write(str(time.time()))


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
    if not done.wait(timeout=600):
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


def write_cache(bundle: dict):
    """把服务器下发的联系人+soul 写成 duplex 可读的本地缓存,建号码/姓名两套索引。"""
    souls = {s["id"]: s for s in bundle.get("souls", [])}
    by_phone, by_name = {}, {}
    for c in bundle.get("contacts", []):
        if c.get("phone"):
            by_phone[c["phone"]] = c
        if c.get("name"):
            by_name[c["name"].strip()] = c
    json.dump({"by_phone": by_phone, "by_name": by_name, "souls": souls,
               "default_greeting": bundle.get("default_greeting", "")},
              open(CACHE, "w"), ensure_ascii=False, indent=2)


async def periodic_sync(ws):
    """定期拉取,拾取网页上对联系人/soul 的改动。"""
    while True:
        await asyncio.sleep(60)
        try:
            await ws.send(json.dumps({"type": "sync_pull"}))
        except Exception:
            return


def _load_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


async def watch_calls(ws):
    """监视 calls/ 新出现的来电记录,把本次要点归档到对应联系人(按号码/姓名经缓存解析)。"""
    reported = set(_load_json(MEM_REPORTED, []))
    # 首次启动以现有记录为基线,不回溯历史
    if not os.path.exists(MEM_REPORTED):
        reported = {os.path.basename(f) for f in glob.glob(f"{CALLS}/*.json")}
        json.dump(list(reported), open(MEM_REPORTED, "w"))
    while True:
        cache = _load_json(CACHE, {"by_phone": {}, "by_name": {}})
        for f in sorted(glob.glob(f"{CALLS}/*.json")):
            base = os.path.basename(f)
            if base in reported:
                continue
            rec = _load_json(f, None)
            reported.add(base)
            if rec:
                summary = (rec.get("summary") or "").strip()
                raw = str(rec.get("number_raw") or rec.get("number") or "")
                channel = rec.get("channel") or ""
                msg = None
                if channel in ("", "cellular"):
                    # 电话:解析规范号码(缓存按姓名/号码找)
                    phone = ""
                    if raw in cache.get("by_name", {}):
                        phone = cache["by_name"][raw].get("phone", "")
                    if not phone and any(ch.isdigit() for ch in raw):
                        phone = raw
                    if summary and phone:
                        msg = {"type": "call_memory", "phone": phone, "name": raw,
                               "channel": "cellular", "summary": summary, "call_id": base}
                elif summary and raw and raw != "微信来电":
                    # App 来电(微信等):按渠道+名字归档,自动建/更新该渠道联系人
                    msg = {"type": "call_memory", "phone": "", "name": raw,
                           "channel": channel, "summary": summary, "call_id": base}
                if msg:
                    try:
                        await ws.send(json.dumps(msg))
                        log(f"通话记忆已归档 → {msg.get('name') or msg.get('phone')} ({channel or 'cellular'})")
                    except Exception:
                        pass
            json.dump(list(reported)[-500:], open(MEM_REPORTED, "w"))
        await asyncio.sleep(3)


async def run(token):
    url = f"{WS_URL}?token={token}"
    async with websockets.connect(url, open_timeout=20, ping_interval=30, ping_timeout=20) as ws:
        hello = json.loads(await ws.recv())
        log("已连上控制面:", hello)
        watcher = asyncio.create_task(watch_done(ws))
        syncer = asyncio.create_task(periodic_sync(ws))
        caller = asyncio.create_task(watch_calls(ws))
        phoner = asyncio.create_task(report_phones(ws))        # 上报控制的 iPhone 状态+渠道
        await maybe_sync_contacts(ws)
        await ws.send(json.dumps({"type": "sync_pull"}))      # 连上即拉一次联系人+soul 缓存
        try:
            while True:
                msg = json.loads(await ws.recv())
                t = msg.get("type")
                if t == "task":
                    tk = msg["task"]
                    fn = write_pending(tk)
                    log(f"收到任务 {tk.get('id','')[:8]} → 写入 pending/{fn}")
                    await ws.send(json.dumps({"type": "report", "task_id": tk["id"], "status": "running"}))
                elif t == "sync":
                    write_cache(msg)
                    log(f"已同步缓存:联系人 {len(msg.get('contacts', []))},soul {len(msg.get('souls', []))}")
                elif t == "switch_phone":
                    pid = msg.get("phone_id")
                    if pid:
                        log(f"收到切换电话指令 → {pid}(即将重启各进程)")
                        do_switch_phone(pid)
                elif t == "channel_config":
                    if apply_channel_config(msg.get("channels", {})):
                        log(f"已应用渠道设置: {list(msg.get('channels', {}).keys())}")
                    await send_phone_info(ws)        # 应用后立刻回报,网页即时反映
                elif t == "contacts_import_done":
                    log(f"通讯录上报完成:共 {msg.get('total')},新增 {msg.get('created')},合并 {msg.get('merged')}")
                    await ws.send(json.dumps({"type": "sync_pull"}))   # 上报后立刻刷新缓存
        finally:
            for x in (watcher, syncer, caller, phoner):
                x.cancel()


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
