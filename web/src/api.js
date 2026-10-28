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
