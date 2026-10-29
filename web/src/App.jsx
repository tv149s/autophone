import React, { useEffect, useRef, useState } from "react";
import { api, setToken, hasToken } from "./api.js";

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || "";

export default function App() {
  const [authed, setAuthed] = useState(hasToken());
  if (!authed) return <Auth onLogin={() => setAuthed(true)} />;
  // 退出时整页刷新,让 Google 登录脚本(GIS)干净地重新初始化,否则再回登录页按钮不重绘
  return <Dashboard onLogout={() => { setToken("", ""); window.location.reload(); }} />;
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
      setToken(r.access_token, r.refresh_token); onLogin();
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
    setToken(r.access_token, r.refresh_token); onLogin();
  });
  const doRegister = run(async () => {
    const r = await api.register(email, password);
    setEnroll(r); setStage("enroll");
  });
  const doEnable = run(async () => {
    const r = await api.enable2fa(email, password, code);
    setToken(r.access_token, r.refresh_token); onLogin();
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
  const [memChannel, setMemChannel] = useState("");   // 跳到「记忆」页时预设的渠道筛选
  const goMemories = (channel = "") => { setMemChannel(channel); setTab("memories"); };
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
        <button className={tab === "memories" ? "active" : ""} onClick={() => setTab("memories")}>记忆</button>
      </div>
      {tab === "tasks" && <Tasks />}
      {tab === "contacts" && <Contacts />}
      {tab === "souls" && <Souls />}
      {tab === "devices" && <Devices goMemories={goMemories} />}
      {tab === "memories" && <Memories initChannel={memChannel} />}
    </div>
  );
}

const CHANNEL_LABELS = {
  cellular: "蜂窝", whatsapp: "WhatsApp", telegram: "Telegram", wechat: "微信",
  facetime: "FaceTime", signal: "Signal", messenger: "Messenger", qq: "QQ",
  discord: "Discord", zoom: "Zoom", skype: "Skype", x: "X", google_voice: "Google Voice",
};
const chLabel = (c) => CHANNEL_LABELS[c] || c || "蜂窝";

function Memories({ initChannel = "" }) {
  const [channel, setChannel] = useState(initChannel);
  const [q, setQ] = useState("");
  const [items, setItems] = useState([]);
  const [counts, setCounts] = useState({});
  const [err, setErr] = useState("");
  useEffect(() => { setChannel(initChannel); }, [initChannel]);
  const load = async () => {
    try { setItems(await api.listMemories(channel, q)); setErr(""); } catch (x) { setErr(x.message); }
  };
  useEffect(() => { api.memoryChannels().then(setCounts).catch(() => {}); }, []);
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [channel]);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const fmt = (t) => { try { return new Date(t).toLocaleString(); } catch { return t; } };
  return (
    <div>
      <div className="spread"><h2>工作记忆</h2><button className="sec" onClick={load}>刷新</button></div>
      <div className="card">
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <button className={channel === "" ? "" : "sec"} onClick={() => setChannel("")}>全部 {total ? `(${total})` : ""}</button>
          {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => (
            <button key={c} className={channel === c ? "" : "sec"} onClick={() => setChannel(c)}>{chLabel(c)} ({n})</button>
          ))}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <input placeholder="按名字搜索" value={q} onChange={(e) => setQ(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && load()} style={{ flex: 1 }} />
          <button className="sec" onClick={load}>搜索</button>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="card">
        {items.length === 0 ? <span className="muted">还没有通话记忆{channel ? `(${chLabel(channel)})` : ""}。接通的来电会自动在这里形成记忆。</span> :
          <table>
            <thead><tr><th>时间</th><th>接听电话 · 渠道</th><th>联系人</th><th>记忆摘要</th><th>操作</th></tr></thead>
            <tbody>
              {items.map((m) => {
                const del = async () => { if (!confirm("删除这条记忆?")) return; try { await api.deleteMemory(m.id); load(); } catch (x) { alert(x.message); } };
                const setPol = async (p) => {
                  const label = { blocked: "拉黑(此人来电直接拒接、不通知)", skip: "跳过(此人来电不接、但通知你)", normal: "恢复正常接听" }[p];
                  if (!confirm(`对「${m.name || m.phone}」:${label}?`)) return;
                  try { await api.setContactPolicy(m.contact_id, p); load(); } catch (x) { alert(x.message); }
                };
                return (
                  <tr key={m.id}>
                    <td className="mono muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>{fmt(m.created_at)}</td>
                    <td style={{ whiteSpace: "nowrap" }}>{m.source || "—"}<span className="muted"> · </span><span className="pill">{chLabel(m.channel)}</span></td>
                    <td>
                      {m.name || m.phone || "未知"}
                      {m.phone && m.name !== m.phone && <div className="mono muted" style={{ fontSize: 12 }}>{m.phone}</div>}
                      {m.policy === "blocked" && <span className="pill off" style={{ marginLeft: 4 }}>已拉黑</span>}
                      {m.policy === "skip" && <span className="pill off" style={{ marginLeft: 4 }}>跳过</span>}
                    </td>
                    <td>{m.summary}</td>
                    <td className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                      {m.policy !== "blocked" && <button className="sec" title="恶意来电:直接拒接不通知" onClick={() => setPol("blocked")}>拉黑</button>}
                      {m.policy !== "skip" && <button className="sec" title="不接此人、但通知你" onClick={() => setPol("skip")}>跳过</button>}
                      {m.policy !== "normal" && <button className="sec" title="恢复正常接听" onClick={() => setPol("normal")}>恢复</button>}
                      <button className="danger" onClick={del}>删除</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>}
      </div>
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
  const [form, setForm] = useState({ name: "", body: "", greeting: "", meta: {} });
  const [busy, setBusy] = useState(false);
  const [dg, setDg] = useState("");
  const [dgMsg, setDgMsg] = useState("");

  useEffect(() => { api.getSettings().then((s) => setDg(s.default_greeting || "")).catch(() => {}); }, []);
  const saveDg = async () => {
    try { await api.updateSettings(dg); setDgMsg("已保存"); setTimeout(() => setDgMsg(""), 2500); } catch (x) { alert(x.message); }
  };

  const save = async () => {
    setBusy(true);
    try {
      const meta = { ...(form.meta || {}), greeting: form.greeting || "" };
      if (editId) await api.updateSoul(editId, { name: form.name, body: form.body, meta });
      else await api.createSoul({ name: form.name, body: form.body, meta });
      setForm({ name: "", body: "", greeting: "", meta: {} }); setEditId(null); reload();
    } catch (x) { alert(x.message); } finally { setBusy(false); }
  };
  const edit = (s) => { setEditId(s.id); setForm({ name: s.name, body: s.body, greeting: (s.meta || {}).greeting || "", meta: s.meta || {} }); window.scrollTo(0, 0); };
  const del = async (id) => { if (!confirm("删除该身份?绑定它的联系人会回退到默认接线员。")) return; try { await api.deleteSoul(id); reload(); } catch (x) { alert(x.message); } };

  return (
    <div>
      <div className="card">
        <h2>默认接听问候语(陌生来电)</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>接通后 AI 说的第一句。留空则用系统默认。改动会同步到设备,下一通生效。</p>
        <textarea value={dg} onChange={(e) => setDg(e.target.value)}
          placeholder="您好,我是张雷的 AI 助理,本通电话可能被录音,请问您是哪位、有什么事?" />
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={saveDg}>保存默认问候语</button>{dgMsg && <span style={{ color: "var(--ok)" }}>✓ {dgMsg}</span>}
        </div>
      </div>

      <div className="card">
        <h2>{editId ? "编辑身份" : "新建身份(Soul)"}</h2>
        <label>名称</label>
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如:法律顾问 / 餐厅客服 / 老友寒暄" />
        <label>接通问候语(此身份接通后说的第一句;留空用默认)</label>
        <input value={form.greeting} onChange={(e) => setForm({ ...form, greeting: e.target.value })} placeholder="如:王先生您好,我是张雷的助理,有什么可以帮您?" />
        <label>身份描述(决定语气、立场、职责;英文书写效果最好)</label>
        <textarea style={{ minHeight: 160 }} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })}
          placeholder="You are ... Be ... Handle ..." />
        <div className="row" style={{ marginTop: 14 }}>
          <button disabled={busy || !form.name} onClick={save}>{editId ? "保存" : "新建"}</button>
          {editId && <button className="sec" onClick={() => { setEditId(null); setForm({ name: "", body: "", greeting: "", meta: {} }); }}>取消</button>}
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
  const [soulSaved, setSoulSaved] = useState(false);
  const soulName = (id) => (souls.find((s) => s.id === id) || {}).name || "";

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
  // 选专属身份即时保存(编辑已有联系人时),不用再找保存按钮
  const onSoulChange = async (e) => {
    const soul_id = e.target.value;
    setForm({ ...form, soul_id });
    if (editId) {
      try {
        await api.updateContact(editId, { ...form, soul_id: soul_id || null });
        setSoulSaved(true); setTimeout(() => setSoulSaved(false), 2500); load();
      } catch (x) { alert(x.message); }
    }
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
    if (editId) {
      try { await api.updateContact(editId, { ...form, soul_id: s.id }); setMsg("专属身份已创建并保存"); load(); } catch (x) { alert(x.message); }
    }
    alert(`已新建专属身份「${s.name}」${editId ? "并保存到该联系人" : "并选中"}。可到「身份」页细化它的描述。`);
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
          <div style={{ flex: 2 }}><label>姓名</label><input value={form.name} onChange={set("name")} placeholder="填写姓名" /></div>
          <div style={{ flex: 2 }}><label>电话</label><input value={form.phone} onChange={set("phone")} placeholder="填写电话号码" /></div>
          <div style={{ flex: 1 }}><label>称谓</label><input value={form.title} onChange={set("title")} placeholder="可选" /></div>
        </div>
        <div className="row">
          <div style={{ flex: 1 }}><label>关系</label>
            <select value={form.relationship} onChange={set("relationship")}>
              {RELATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <div style={{ flex: 1 }}><label>生日</label><input value={form.birthday || ""} onChange={set("birthday")} placeholder="可选,格式 1980-05-01 或 05-01" /></div>
          <div style={{ flex: 2 }}><label>单位/机构</label><input value={form.org} onChange={set("org")} placeholder="可选" /></div>
        </div>
        <label>喜好</label><input value={form.preferences} onChange={set("preferences")} placeholder="可选,如接待偏好、忌口等" />
        <label>备注</label><textarea value={form.notes} onChange={set("notes")} />
        <label>专属身份(来电时用这个 Soul 接听;不选则用默认接线员){editId && <span className="muted"> · 选择即自动保存</span>}{soulSaved && <span style={{ color: "var(--ok)" }}> ✓ 已保存</span>}</label>
        <div className="row">
          <select style={{ flex: 1 }} value={form.soul_id || ""} onChange={onSoulChange}>
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
          <thead><tr><th>姓名</th><th>电话</th><th>关系</th><th>专属身份</th><th>生日</th><th></th></tr></thead>
          <tbody>
            {list.map((c) => (
              <tr key={c.id}>
                <td>{c.title && <span className="muted">{c.title} </span>}{c.name || <span className="muted">(无名)</span>}{c.channel && <span className="pill off" style={{ marginLeft: 6 }}>{{ wechat: "微信", telegram: "Telegram", qq: "QQ" }[c.channel] || c.channel}</span>}</td>
                <td className="mono">{c.phone || <span className="muted">{c.channel ? "App 联系人" : "—"}</span>}</td>
                <td>{c.relationship ? <span className="pill off">{relLabel(c.relationship)}</span> : <span className="muted">—</span>}</td>
                <td>{c.soul_id ? <span className="pill on">{soulName(c.soul_id) || "已设"}</span> : <span className="muted">默认</span>}</td>
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

function Devices({ goMemories }) {
  const [devices, err, reload] = useAsync(() => api.listDevices());
  const [phones, setPhones] = useState([]);
  const loadPhones = () => api.listPhones().then(setPhones).catch(() => {});
  // 自动刷新:在设备上登录客户端后会自动出现,无需手动创建
  useEffect(() => {
    loadPhones();
    const t = setInterval(() => { reload(); loadPhones(); }, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line
  }, []);

  const toggleAuto = async (p) => {
    try { await api.setPhoneAutoAnswer(p.id, !p.auto_answer); loadPhones(); } catch (x) { alert(x.message); }
  };
  // 监控集合:默认单机(1台),加第 2 台进入双机监控(忙时占线跳过),最多 2 台、至少 1 台
  const toggleMonitor = async (d, ph) => {
    const cur = (d.phone_registry || []).filter((x) => x.monitored).map((x) => x.id);
    let next;
    if (ph.monitored) {
      if (cur.length <= 1) { alert("至少保留一台监控电话。"); return; }
      next = cur.filter((id) => id !== ph.id);
    } else {
      if (ph.ready === false) { alert("该机控制端未就绪,不能加入监控。"); return; }
      if (cur.length >= 2) { alert("最多同时监控 2 台(双机)。请先停用一台再加。"); return; }
      next = [...cur, ph.id];
    }
    if (!confirm(`把监控集合设为「${next.join(", ")}」?\n会重启控制进程(约 30-60 秒,设备短暂离线)。`)) return;
    try { await api.setMonitor(d.id, next); alert(`已下发监控集合「${next.join(", ")}」。重启约 30-60 秒。`); }
    catch (x) { alert(x.message); }
  };
  const toggleChannel = async (p, c) => {
    try { await api.setPhoneChannel(p.id, c.kind, { enabled: !c.enabled }); loadPhones(); } catch (x) { alert(x.message); }
  };

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
          <thead><tr><th>名称</th><th>类型</th><th>在线</th><th>监控中</th><th></th></tr></thead>
          <tbody>
            {(devices || []).map((d) => (
              <tr key={d.id}>
                <td>{d.name || <span className="muted">(未命名)</span>}<div className="mono muted" style={{ fontSize: 12 }}>{d.hostname || ""}</div></td>
                <td className="mono">{d.kind}</td>
                <td><span className={"pill " + (d.online ? "on" : "off")}>{d.online ? "在线" : "离线"}</span></td>
                <td>
                  {(() => {
                    const mon = (d.phone_registry || []).filter((ph) => ph.monitored);
                    return mon.length ? <span>{mon.map((m) => m.label).join(" · ")}{mon.length > 1 && <span className="pill on" style={{ marginLeft: 6 }}>双机</span>}</span>
                                      : <span className="muted">—</span>;
                  })()}
                  <div className="mono muted" style={{ fontSize: 12 }}>在下方卡片切换监控</div>
                </td>
                <td className="row">
                  <button className="sec" onClick={() => rename(d)}>改名</button>
                  <button className="danger" onClick={() => del(d.id)}>移除</button>
                </td>
              </tr>
            ))}
            {devices && devices.length === 0 && <tr><td colSpan={5} className="muted">还没有设备。在设备上登录客户端即可自动接入。</td></tr>}
          </tbody>
        </table>
      </div>

      <h2>电话</h2>
      {(devices || []).every((d) => (d.phone_registry || []).length === 0) &&
        <div className="card"><span className="muted">边缘端上线后会自动上报它控制的电话(约 30 秒内出现)。</span></div>}
      {(devices || []).map((d) => (d.phone_registry || []).length === 0 ? null : (
        <div key={d.id} style={{ marginBottom: 18 }}>
          <div className="muted" style={{ margin: "4px 0 8px" }}>
            由 <b style={{ color: "var(--text)" }}>{d.name || "设备"}</b> 控制
            <span className={"pill " + (d.online ? "on" : "off")} style={{ marginLeft: 8 }}>{d.online ? "在线" : "离线"}</span>
          </div>
          <div className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>
            默认单机;给第 2 台点「加入监控」进入双机监控(同时监控两台,忙时只接一通、其余占线跳过并通知)。最多 2 台。
          </div>
          <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-start" }}>
            {d.phone_registry.map((ph) => {
              const live = ph.monitored ? phones.find((p) => p.device_id === d.id && p.pkey === ph.id) : null;
              return (
                <div className="card" key={ph.id}
                     style={{ flex: ph.monitored ? "1 1 100%" : "0 1 240px",
                              border: ph.monitored ? "2px solid var(--accent, #3a9d7a)" : undefined,
                              opacity: ph.monitored ? 1 : 0.9 }}>
                  <div className="spread">
                    <div>
                      <b>{ph.label}</b>
                      {ph.monitored && <span className="pill on" style={{ marginLeft: 8 }}>监控中</span>}
                      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{ph.number || ""} · {ph.platform}</div>
                    </div>
                    <button className="sec" disabled={!d.online || (!ph.monitored && ph.ready === false)}
                            title={ph.ready === false ? "该机控制端未就绪" : (ph.monitored ? "停止监控此机" : "加入监控")}
                            onClick={() => toggleMonitor(d, ph)}>
                      {ph.monitored ? "停止监控" : (ph.ready === false ? "未就绪" : "加入监控")}
                    </button>
                  </div>
                  {ph.monitored && live && (
                    <>
                      <div className="row" style={{ marginTop: 8 }}>
                        <span className={"pill " + (live.wda_ready ? "on" : "off")}>{live.wda_ready ? "控制就绪" : "未就绪"}</span>
                        <span className={"pill " + (live.locked ? "off" : "on")}>{live.locked ? (ph.platform === "android" ? "熄屏" : "锁屏") : "亮屏"}</span>
                        {live.front_app && <span className="pill off">前台:{live.front_app}</span>}
                        <button className="sec" onClick={() => toggleAuto(live)}>{live.auto_answer ? "自动接听:开" : "自动接听:关"}</button>
                      </div>
                      <table style={{ marginTop: 10 }}>
                        <thead><tr><th>应用/渠道</th><th>可接听</th><th>可外呼</th><th>状态</th><th>启用</th><th>记忆</th></tr></thead>
                        <tbody>
                          {(live.channels || []).filter((c) => c.enabled).map((c) => (
                            <tr key={c.kind}>
                              <td>{c.name || c.kind}{c.warn && <div className="err" style={{ fontSize: 12, marginTop: 2 }}>⚠️ {c.warn}</div>}</td>
                              <td>{c.answer ? <span className="pill on">接听</span> : <span className="muted">—</span>}</td>
                              <td>{c.outbound ? <span className="pill on">外呼</span> : <span className="muted">—</span>}</td>
                              <td><span className={"pill " + (c.status === "supported" ? "on" : "off")}>{{ supported: "已测通", testing: "测试中", untested: "未测", unsupported: "不支持" }[c.status] || c.status}</span></td>
                              <td>
                                <button className={c.enabled ? "" : "sec"}
                                        disabled={c.status !== "supported" && !c.enabled}
                                        title={c.status !== "supported" ? "仅已测通的渠道建议开启" : ""}
                                        onClick={() => toggleChannel(live, c)}>
                                  {c.enabled ? "已启用" : "已关闭"}
                                </button>
                              </td>
                              <td>
                                <button className="sec" title="查看该渠道的通话记忆"
                                        onClick={() => goMemories && goMemories(c.kind)}>记忆</button>
                              </td>
                            </tr>
                          ))}
                          {(live.channels || []).filter((c) => c.enabled).length === 0 && <tr><td colSpan={6} className="muted">暂无已开通渠道</td></tr>}
                        </tbody>
                      </table>
                    </>
                  )}
                  {ph.monitored && !live && <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>正在获取实时状态…</div>}
                </div>
              );
            })}
          </div>
          {(d.phone_registry || []).filter((x) => x.monitored).length >= 2 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              📵 双机监控:两台同时监控,忙时只接一通;另一通不接也不挂,只记「占线未接」并通知。音频经 QQFM 混音器合流到 Mac。
            </div>
          )}
        </div>
      ))}
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

  // 设备加载后默认选中:上次用的(若仍在) → 否则第一个在线 → 否则第一个
  useEffect(() => {
    if (!devices || deviceId) return;
    const last = localStorage.getItem("ap_last_device");
    const pick = (last && devices.find((d) => d.id === last)) ||
                 devices.find((d) => d.online) || devices[0];
    if (pick) setDeviceId(pick.id);
    // eslint-disable-next-line
  }, [devices]);

  const chooseDevice = (e) => { setDeviceId(e.target.value); if (e.target.value) localStorage.setItem("ap_last_device", e.target.value); };

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
        <label>目标电话(E.164 格式,如 +14255550123)</label>
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="填写目标电话号码" />
        <label>任务目标</label>
        <textarea value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="描述要办的事" />
        <label>指派设备</label>
        <select value={deviceId} onChange={chooseDevice}>
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
