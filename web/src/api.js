// API 客户端。同源部署时走相对 /api 前缀,否则用 api. 子域。
const BASE = import.meta.env.VITE_API_BASE || "https://api.autophoneagent.com";

let token = localStorage.getItem("ap_token") || "";
let refresh = localStorage.getItem("ap_refresh") || "";

// 登录/刷新成功后保存令牌对;access 必填,refresh 可选(刷新时后端会轮换出新的)
export function setToken(t, r) {
  token = t || "";
  if (t) localStorage.setItem("ap_token", t);
  else localStorage.removeItem("ap_token");
  if (r !== undefined) {
    refresh = r || "";
    if (r) localStorage.setItem("ap_refresh", r);
    else localStorage.removeItem("ap_refresh");
  }
}
export function hasToken() {
  return !!token;
}

async function rawReq(path, { method = "GET", body, headers = {} } = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  return { res, data };
}

let refreshing = null;   // 并发请求同时 401 时,只发一次刷新
async function doRefresh() {
  if (!refresh) return false;
  if (!refreshing) {
    refreshing = rawReq("/auth/refresh", { method: "POST", body: { refresh_token: refresh } })
      .then(({ res, data }) => {
        if (res.ok && data.access_token) {
          setToken(data.access_token, data.refresh_token);
          return true;
        }
        return false;
      })
      .catch(() => false)
      .finally(() => { setTimeout(() => (refreshing = null), 0); });
  }
  return refreshing;
}

async function req(path, opts = {}) {
  const { method = "GET", body, auth = false, bearer } = opts;
  const b = bearer ?? (auth ? token : null);
  const hdr = b ? { Authorization: `Bearer ${b}` } : {};
  let { res, data } = await rawReq(path, { method, body, headers: hdr });
  // access 过期 → 用 refresh 静默换新后重试一次(只对带登录态的请求,且非 auth 接口自身)
  if (res.status === 401 && auth && !path.startsWith("/auth/")) {
    if (await doRefresh()) {
      ({ res, data } = await rawReq(path, { method, body, headers: { Authorization: `Bearer ${token}` } }));
    } else {
      setToken("", "");               // refresh 也失效 → 清空,回登录页
      window.location.reload();
      throw new Error("登录已过期,请重新登录");
    }
  }
  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return data;
}

export const api = {
  register: (email, password) => req("/auth/register", { method: "POST", body: { email, password } }),
  enable2fa: (email, password, code) =>
    req("/auth/2fa/enable", { method: "POST", body: { email, password, code } }),
  login: (email, password) => req("/auth/login", { method: "POST", body: { email, password } }),
  googleLogin: (credential) => req("/auth/google", { method: "POST", body: { credential } }),
  verify2fa: (mfaToken, code) =>
    req("/auth/2fa/verify", { method: "POST", body: { code }, bearer: mfaToken }),

  listDevices: () => req("/devices", { auth: true }),
  renameDevice: (id, name) => req(`/devices/${id}`, { method: "PATCH", auth: true, body: { name } }),
  deleteDevice: (id) => req(`/devices/${id}`, { method: "DELETE", auth: true }),
  switchPhone: (deviceId, phone_id) => req(`/devices/${deviceId}/switch-phone`, { method: "POST", auth: true, body: { phone_id } }),
  listPhones: () => req("/phones", { auth: true }),
  setPhoneAutoAnswer: (id, auto_answer) => req(`/phones/${id}`, { method: "PATCH", auth: true, body: { auto_answer } }),
  setPhoneChannel: (id, kind, patch) => req(`/phones/${id}/channel`, { method: "PATCH", auth: true, body: { kind, ...patch } }),

  getSettings: () => req("/settings", { auth: true }),
  updateSettings: (default_greeting) => req("/settings", { method: "PATCH", auth: true, body: { default_greeting } }),
  listSouls: () => req("/souls", { auth: true }),
  createSoul: (data) => req("/souls", { method: "POST", auth: true, body: data }),
  updateSoul: (id, data) => req(`/souls/${id}`, { method: "PATCH", auth: true, body: data }),
  deleteSoul: (id) => req(`/souls/${id}`, { method: "DELETE", auth: true }),

  listContacts: (q = "") => req(`/contacts${q ? `?q=${encodeURIComponent(q)}` : ""}`, { auth: true }),
  getContact: (id) => req(`/contacts/${id}`, { auth: true }),
  createContact: (data) => req("/contacts", { method: "POST", auth: true, body: data }),
  updateContact: (id, data) => req(`/contacts/${id}`, { method: "PATCH", auth: true, body: data }),
  deleteContact: (id) => req(`/contacts/${id}`, { method: "DELETE", auth: true }),
  importContacts: (kind, text) => req("/contacts/import", { method: "POST", auth: true, body: { kind, text } }),

  listTasks: () => req("/tasks", { auth: true }),
  createTask: (device_id, card) =>
    req("/tasks", { method: "POST", auth: true, body: { device_id, card } }),
  cancelTask: (id) => req(`/tasks/${id}/cancel`, { method: "POST", auth: true }),
};
