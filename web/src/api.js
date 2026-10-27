// API 客户端。同源部署时走相对 /api 前缀,否则用 api. 子域。
const BASE = import.meta.env.VITE_API_BASE || "https://api.autophoneagent.com";

let token = localStorage.getItem("ap_token") || "";

export function setToken(t) {
  token = t || "";
  if (t) localStorage.setItem("ap_token", t);
  else localStorage.removeItem("ap_token");
}
export function hasToken() {
  return !!token;
}

async function req(path, { method = "GET", body, auth = false, bearer } = {}) {
  const headers = { "Content-Type": "application/json" };
  const b = bearer ?? (auth ? token : null);
  if (b) headers["Authorization"] = `Bearer ${b}`;
  const res = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
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

  listTasks: () => req("/tasks", { auth: true }),
  createTask: (device_id, card) =>
    req("/tasks", { method: "POST", auth: true, body: { device_id, card } }),
  cancelTask: (id) => req(`/tasks/${id}/cancel`, { method: "POST", auth: true }),
};
