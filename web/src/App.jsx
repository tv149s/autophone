import React, { useEffect, useRef, useState } from "react";
import { api, setToken, hasToken } from "./api.js";

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || "";

export default function App() {
  const [authed, setAuthed] = useState(hasToken());
  if (!authed) return <Auth onLogin={() => setAuthed(true)} />;
  // 退出时整页刷新,让 Google 登录脚本(GIS)干净地重新初始化,否则再回登录页按钮不重绘
  return <Dashboard onLogout={() => { setToken(""); window.location.reload(); }} />;
}

/* Google Identity Services 登录按钮 */
function GoogleButton({ onToken, onError }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return;
    let tries = 0;
    let timer = null;
    const tick = () => {
      if (window.google?.accounts?.id) {
        window.google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: (resp) => onToken(resp.credential),
        });
        if (ref.current) {
          window.google.accounts.id.renderButton(ref.current, {
            theme: "filled_blue", size: "large", width: 320, text: "continue_with",
          });
        }
        return;
      }
      if (++tries > 50) {        // 等约 10 秒仍没加载到 → 多半被浏览器拦截
        onError?.("无法加载 Google 登录(可能被浏览器的隐私保护/广告拦截插件挡住了)");
        return;
      }
      timer = setTimeout(tick, 200);
    };
    tick();
    return () => timer && clearTimeout(timer);
  }, []);
  if (!GOOGLE_CLIENT_ID)
    return <div className="muted" style={{ fontSize: 13 }}>Google 登录未配置(缺 Client ID)</div>;
  return <div ref={ref} style={{ display: "flex", justifyContent: "center" }} />;
}

/* ---------------- 登录 / 注册 / 2FA ---------------- */
function Auth({ onLogin }) {
  const [mode, setMode] = useState("login"); // login | register
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [stage, setStage] = useState("creds"); // creds | mfa | enroll
  const [mfaToken, setMfaToken] = useState("");
  const [enroll, setEnroll] = useState(null); // {totp_secret, totp_uri}
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [showEmail, setShowEmail] = useState(false);

  const doGoogle = async (credential) => {
    setErr("");
    try {
      const r = await api.googleLogin(credential);
      setToken(r.access_token); onLogin();
    } catch (x) { setErr(x.message); }
  };

  const run = (fn) => async (e) => {
    e?.preventDefault();
    setErr(""); setBusy(true);
    try { await fn(); } catch (x) { setErr(x.message); } finally { setBusy(false); }
  };

  const doLogin = run(async () => {
    const r = await api.login(email, password);
    if (r.mfa_required) { setMfaToken(r.mfa_token); setStage("mfa"); }
  });
  const doVerify = run(async () => {
    const r = await api.verify2fa(mfaToken, code);
    setToken(r.access_token); onLogin();
  });
  const doRegister = run(async () => {
    const r = await api.register(email, password);
    setEnroll(r); setStage("enroll");
  });
  const doEnable = run(async () => {
    const r = await api.enable2fa(email, password, code);
    setToken(r.access_token); onLogin();
  });

  return (
    <div className="center">
      <h1>AutoPhoneAgent 控制台</h1>
      {stage === "creds" && (
        <div className="card">
          <p className="muted" style={{ marginTop: 0 }}>推荐用 Google 账户登录</p>
          <GoogleButton onToken={doGoogle} onError={setErr} />
          <div style={{ textAlign: "center", margin: "16px 0 4px" }}>
            <a href="#" onClick={(e) => { e.preventDefault(); setShowEmail(!showEmail); setErr(""); }}>
              {showEmail ? "收起邮箱登录" : "用邮箱登录 / 注册"}
            </a>
          </div>
          {err && !showEmail && <div className="err">{err}</div>}
          {showEmail && (
        <form onSubmit={mode === "login" ? doLogin : doRegister}>
          <div className="tabs">
            <button type="button" className={mode === "login" ? "active" : ""} onClick={() => { setMode("login"); setErr(""); }}>登录</button>
            <button type="button" className={mode === "register" ? "active" : ""} onClick={() => { setMode("register"); setErr(""); }}>注册</button>
          </div>
          <label>邮箱</label>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
          <label>密码{mode === "register" ? "(至少 8 位)" : ""}</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
          <div style={{ marginTop: 16 }}>
            <button disabled={busy}>{mode === "login" ? "登录" : "注册并设置二次验证"}</button>
          </div>
          {err && <div className="err">{err}</div>}
        </form>
          )}
        </div>
      )}
      {stage === "mfa" && (
        <form className="card" onSubmit={doVerify}>
          <p className="muted">请输入验证器 App 上的 6 位验证码</p>
          <input inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} placeholder="000000" />
          <div style={{ marginTop: 16 }}><button disabled={busy}>验证登录</button></div>
          {err && <div className="err">{err}</div>}
        </form>
      )}
      {stage === "enroll" && enroll && (
        <form className="card" onSubmit={doEnable}>
          <p>用验证器 App(Google Authenticator / 1Password 等)扫码,或手动输入密钥:</p>
          <label>密钥(手动输入用)</label>
          <div className="token mono">{enroll.totp_secret}</div>
          <label>otpauth 链接</label>
          <div className="token mono">{enroll.totp_uri}</div>
          <label>输入验证器显示的 6 位码以完成绑定</label>
          <input inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} placeholder="000000" />
          <div style={{ marginTop: 16 }}><button disabled={busy}>启用并进入</button></div>
          {err && <div className="err">{err}</div>}
        </form>
      )}
    </div>
  );
}

/* ---------------- 控制台 ---------------- */
function Dashboard({ onLogout }) {
  const [tab, setTab] = useState("tasks");
  return (
    <div className="wrap">
      <div className="topbar">
        <h1>AutoPhoneAgent</h1>
        <button className="sec" onClick={onLogout}>退出</button>
      </div>
      <div className="tabs">
        <button className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}>任务</button>
        <button className={tab === "contacts" ? "active" : ""} onClick={() => setTab("contacts")}>通讯录</button>
        <button className={tab === "souls" ? "active" : ""} onClick={() => setTab("souls")}>身份</button>
        <button className={tab === "devices" ? "active" : ""} onClick={() => setTab("devices")}>设备</button>
      </div>
      {tab === "tasks" && <Tasks />}
      {tab === "contacts" && <Contacts />}
      {tab === "souls" && <Souls />}
      {tab === "devices" && <Devices />}
    </div>
  );
}

function useAsync(fn, deps = []) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const reload = async () => { try { setData(await fn()); setErr(""); } catch (x) { setErr(x.message); } };
  useEffect(() => { reload(); /* eslint-disable-next-line */ }, deps);
  return [data, err, reload];
}

function Souls() {
  const [souls, err, reload] = useAsync(() => api.listSouls());
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({ name: "", body: "" });
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      if (editId) await api.updateSoul(editId, { ...form, meta: {} });
      else await api.createSoul({ ...form, meta: {} });
      setForm({ name: "", body: "" }); setEditId(null); reload();
    } catch (x) { alert(x.message); } finally { setBusy(false); }
  };
  const edit = (s) => { setEditId(s.id); setForm({ name: s.name, body: s.body }); window.scrollTo(0, 0); };
  const del = async (id) => { if (!confirm("删除该身份?绑定它的联系人会回退到默认接线员。")) return; try { await api.deleteSoul(id); reload(); } catch (x) { alert(x.message); } };

  return (
    <div>
      <div className="card">
        <h2>{editId ? "编辑身份" : "新建身份(Soul)"}</h2>
        <label>名称</label>
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如:法律顾问 / 餐厅客服 / 老友寒暄" />
        <label>身份描述(决定语气、立场、职责;英文书写效果最好)</label>
        <textarea style={{ minHeight: 160 }} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })}
          placeholder="You are ... Be ... Handle ..." />
        <div className="row" style={{ marginTop: 14 }}>
          <button disabled={busy || !form.name} onClick={save}>{editId ? "保存" : "新建"}</button>
          {editId && <button className="sec" onClick={() => { setEditId(null); setForm({ name: "", body: "" }); }}>取消</button>}
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>名称</th><th>描述摘要</th><th></th></tr></thead>
          <tbody>
            {(souls || []).map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td className="muted">{(s.body || "").slice(0, 80)}{(s.body || "").length > 80 ? "…" : ""}</td>
                <td className="row">
                  <button className="sec" onClick={() => edit(s)}>编辑</button>
                  <button className="danger" onClick={() => del(s.id)}>删除</button>
                </td>
              </tr>
            ))}
            {souls && souls.length === 0 && <tr><td colSpan={3} className="muted">加载中…</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const RELATIONS = [
  ["", "未分类"], ["friend", "朋友"], ["colleague", "同事"], ["customer", "客户"],
  ["family", "家人"], ["vendor", "供应商"], ["org", "机构"], ["other", "其他"],
];
const relLabel = (r) => (RELATIONS.find((x) => x[0] === r) || ["", r])[1];

const EMPTY_C = { name: "", phone: "", title: "", relationship: "", birthday: "", org: "", preferences: "", notes: "", soul_id: "" };

function Contacts() {
  const [q, setQ] = useState("");
  const [list, setList] = useState([]);
  const [souls, setSouls] = useState([]);
  const [err, setErr] = useState("");
  const [form, setForm] = useState(EMPTY_C);
  const [editId, setEditId] = useState(null);
  const [memories, setMemories] = useState([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = async (query = q) => {
    try { setList(await api.listContacts(query)); setErr(""); } catch (x) { setErr(x.message); }
  };
  useEffect(() => {
    load("");
    api.listSouls().then(setSouls).catch(() => {});
    // eslint-disable-next-line
  }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const resetForm = () => { setForm(EMPTY_C); setEditId(null); setMemories([]); };

  const save = async () => {
    setBusy(true);
    try {
      const payload = { ...form, soul_id: form.soul_id || null };
      if (editId) await api.updateContact(editId, payload);
      else await api.createContact(payload);
      resetForm(); load();
    } catch (x) { alert(x.message); } finally { setBusy(false); }
  };
  const newDedicatedSoul = async () => {
    const base = form.name || "联系人";
    const s = await api.createSoul({
      name: `专属·${base}`,
      body: `You are the owner's assistant speaking with ${base}. Be warm and personal, remember what matters to them, and handle their requests the way the owner would.`,
      meta: {},
    });
    setSouls(await api.listSouls());
    setForm({ ...form, soul_id: s.id });
    alert(`已新建专属身份「${s.name}」并选中。可到「身份」页细化它的描述。`);
  };
  const edit = async (c) => {
    setEditId(c.id); setForm({ ...EMPTY_C, ...c, birthday: c.birthday || "", soul_id: c.soul_id || "" });
    setMemories([]); window.scrollTo(0, 0);
    try { const d = await api.getContact(c.id); setMemories(d.memories || []); } catch { /* ignore */ }
  };
  const del = async (id) => { if (!confirm("删除该联系人?")) return; try { await api.deleteContact(id); load(); } catch (x) { alert(x.message); } };

  const onFile = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const text = await f.text();
    const kind = f.name.toLowerCase().endsWith(".csv") ? "csv" : "vcard";
    setBusy(true); setMsg("");
    try {
      const r = await api.importContacts(kind, text);
      setMsg(`导入完成:共 ${r.total} 条,新增 ${r.created},合并 ${r.merged}`);
      load();
    } catch (x) { alert(x.message); } finally { setBusy(false); e.target.value = ""; }
  };

  return (
    <div>
      <div className="card">
        <h2>{editId ? "编辑联系人" : "添加联系人"}</h2>
        <div className="row">
          <div style={{ flex: 2 }}><label>姓名</label><input value={form.name} onChange={set("name")} placeholder="王英雄" /></div>
          <div style={{ flex: 2 }}><label>电话</label><input value={form.phone} onChange={set("phone")} placeholder="+1 425 555 0123" /></div>
          <div style={{ flex: 1 }}><label>称谓</label><input value={form.title} onChange={set("title")} placeholder="先生/女士" /></div>
        </div>
        <div className="row">
          <div style={{ flex: 1 }}><label>关系</label>
            <select value={form.relationship} onChange={set("relationship")}>
              {RELATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <div style={{ flex: 1 }}><label>生日</label><input value={form.birthday || ""} onChange={set("birthday")} placeholder="1980-05-01 或 05-01" /></div>
          <div style={{ flex: 2 }}><label>单位/机构</label><input value={form.org} onChange={set("org")} /></div>
        </div>
        <label>喜好</label><input value={form.preferences} onChange={set("preferences")} placeholder="喜欢靠窗、不吃辣…" />
        <label>备注</label><textarea value={form.notes} onChange={set("notes")} />
        <label>专属身份(来电时用这个 Soul 接听;不选则用默认接线员)</label>
        <div className="row">
          <select style={{ flex: 1 }} value={form.soul_id || ""} onChange={set("soul_id")}>
            <option value="">(默认接线员)</option>
            {souls.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <button className="sec" type="button" onClick={newDedicatedSoul}>+ 新建专属 Soul</button>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button disabled={busy || (!form.name && !form.phone)} onClick={save}>{editId ? "保存修改" : "添加"}</button>
          {editId && <button className="sec" onClick={resetForm}>取消</button>}
        </div>
        {editId && (
          <div style={{ marginTop: 16 }}>
            <label>通话记忆(越用越全)</label>
            {memories.length === 0 && <div className="muted" style={{ fontSize: 13 }}>还没有通话记忆</div>}
            {memories.map((m, i) => (
              <div key={i} className="token" style={{ marginBottom: 6, borderStyle: "solid", borderColor: "var(--border)" }}>
                <div className="muted" style={{ fontSize: 12 }}>{m.created_at ? new Date(m.created_at).toLocaleString() : ""}</div>
                <div>{m.summary}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <h2>导入通讯录</h2>
        <p className="muted" style={{ marginTop: 0 }}>支持 <b style={{ color: "var(--text)" }}>.vcf(vCard)</b> 和 <b style={{ color: "var(--text)" }}>.csv</b>。手机/Google/Outlook 均可导出成这两种格式。号码会自动归一化、按号码去重合并。</p>
        <input type="file" accept=".vcf,.csv,text/vcard,text/csv" onChange={onFile} disabled={busy} />
        {msg && <div className="ok">{msg}</div>}
      </div>

      <div className="spread">
        <h2>联系人（{list.length}）</h2>
        <div className="row">
          <input style={{ width: 200 }} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} placeholder="搜索姓名/号码/机构" />
          <button className="sec" onClick={() => load()}>搜索</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>姓名</th><th>电话</th><th>关系</th><th>机构</th><th>生日</th><th></th></tr></thead>
          <tbody>
            {list.map((c) => (
              <tr key={c.id}>
                <td>{c.title && <span className="muted">{c.title} </span>}{c.name || <span className="muted">(无名)</span>}</td>
                <td className="mono">{c.phone || <span className="muted">—</span>}</td>
                <td>{c.relationship ? <span className="pill off">{relLabel(c.relationship)}</span> : <span className="muted">—</span>}</td>
                <td className="muted">{c.org || "—"}</td>
                <td className="muted">{c.birthday || "—"}</td>
                <td className="row">
                  <button className="sec" onClick={() => edit(c)}>编辑</button>
                  <button className="danger" onClick={() => del(c.id)}>删除</button>
                </td>
              </tr>
            ))}
            {list.length === 0 && <tr><td colSpan={6} className="muted">还没有联系人。手动添加或导入 vCard/CSV。</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Devices() {
  const [devices, err, reload] = useAsync(() => api.listDevices());
  // 自动刷新:在设备上登录客户端后会自动出现,无需手动创建
  useEffect(() => {
    const t = setInterval(reload, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line
  }, []);

  const del = async (id) => {
    if (!confirm("移除该设备?移除后它将无法接任务,需在该设备上重新登录客户端。")) return;
    try { await api.deleteDevice(id); reload(); } catch (x) { alert(x.message); }
  };
  const rename = async (d) => {
    const name = prompt("设备名称", d.name || "");
    if (name == null) return;
    try { await api.renameDevice(d.id, name); reload(); } catch (x) { alert(x.message); }
  };

  return (
    <div>
      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          在设备上登录客户端后,设备会<b style={{ color: "var(--text)" }}>自动出现</b>在这里并接入,无需手动创建。
        </p>
      </div>
      <div className="spread"><h2>我的设备</h2><button className="sec" onClick={reload}>刷新</button></div>
      {err && <div className="err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>名称</th><th>类型</th><th>主机名</th><th>在线</th><th>最后在线</th><th></th></tr></thead>
          <tbody>
            {(devices || []).map((d) => (
              <tr key={d.id}>
                <td>{d.name || <span className="muted">(未命名)</span>}</td>
                <td className="mono">{d.kind}</td>
                <td className="mono muted">{d.hostname || "—"}</td>
                <td><span className={"pill " + (d.online ? "on" : "off")}>{d.online ? "在线" : "离线"}</span></td>
                <td className="muted">{d.last_seen ? new Date(d.last_seen).toLocaleString() : "—"}</td>
                <td className="row">
                  <button className="sec" onClick={() => rename(d)}>改名</button>
                  <button className="danger" onClick={() => del(d.id)}>移除</button>
                </td>
              </tr>
            ))}
            {devices && devices.length === 0 && <tr><td colSpan={6} className="muted">还没有设备。在设备上登录客户端即可自动接入。</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Tasks() {
  const [tasks, err, reload] = useAsync(() => api.listTasks());
  const [devices] = useAsync(() => api.listDevices());
  const [deviceId, setDeviceId] = useState("");
  const [to, setTo] = useState("");
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    setBusy(true);
    try {
      await api.createTask(deviceId || null, { goal, to });
      setGoal(""); setTo(""); reload();
    } catch (x) { alert(x.message); } finally { setBusy(false); }
  };

  return (
    <div>
      <div className="card">
        <h2>下达外呼任务</h2>
        <label>目标电话(E.164,如 +14255550123)</label>
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="+1..." />
        <label>任务目标</label>
        <textarea value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="例如:打给金龙饭店,订明晚 7 点两人桌" />
        <label>指派设备</label>
        <select value={deviceId} onChange={(e) => setDeviceId(e.target.value)}>
          <option value="">(不指定)</option>
          {(devices || []).map((d) => <option key={d.id} value={d.id}>{d.name || d.id} {d.online ? "· 在线" : "· 离线"}</option>)}
        </select>
        <div style={{ marginTop: 14 }}><button disabled={busy || !goal} onClick={add}>下达任务</button></div>
      </div>
      <div className="spread"><h2>任务列表</h2><button className="sec" onClick={reload}>刷新</button></div>
      {err && <div className="err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>时间</th><th>目标</th><th>号码</th><th>状态</th><th>结果</th></tr></thead>
          <tbody>
            {(tasks || []).map((t) => (
              <tr key={t.id}>
                <td className="muted">{new Date(t.created_at).toLocaleString()}</td>
                <td>{t.card?.goal || "—"}</td>
                <td className="mono">{t.card?.to || "—"}</td>
                <td><span className={"pill " + t.status}>{t.status}</span></td>
                <td className="muted">{t.result ? (t.result.status || "") + " " + (t.result.note || "") : "—"}</td>
              </tr>
            ))}
            {tasks && tasks.length === 0 && <tr><td colSpan={5} className="muted">还没有任务</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
