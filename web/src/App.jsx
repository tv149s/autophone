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
        <button className={tab === "devices" ? "active" : ""} onClick={() => setTab("devices")}>设备</button>
      </div>
      {tab === "tasks" ? <Tasks /> : <Devices />}
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
