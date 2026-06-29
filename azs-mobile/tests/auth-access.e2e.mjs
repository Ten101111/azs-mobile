import { chromium } from "playwright";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const appDir = path.resolve(__dirname, "..");
const baseURL = process.env.E2E_BASE_URL || "http://localhost:5174";
const password = "Password123";

function fail(message) {
  throw new Error(message);
}

async function api(pathname, options = {}) {
  const response = await fetch(`${baseURL}${pathname}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  return { response, text };
}

function sqlite(statement, params = []) {
  const script = [
    "import sqlite3, sys",
    "db = sys.argv[1]",
    "statement = sys.argv[2]",
    "params = sys.argv[3:]",
    "conn = sqlite3.connect(db)",
    "conn.execute(statement, params)",
    "conn.commit()",
    "conn.close()",
  ].join("; ");
  const result = spawnSync("python3", ["-c", script, path.join(appDir, "data", "auth.sqlite3"), statement, ...params], {
    encoding: "utf-8",
  });
  if (result.status !== 0) fail(result.stderr || result.stdout || "sqlite command failed");
}

async function expectStatus(pathname, status) {
  const { response, text } = await api(pathname);
  if (response.status !== status) fail(`${pathname}: expected ${status}, got ${response.status}: ${text}`);
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    fail(`expected JSON, got: ${text}`);
  }
}

async function run() {
  await expectStatus("/api/stations", 401);

  const externalEmail = `external-${Date.now()}@example.test`;
  const blocked = await api("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email: externalEmail, password, name: "External User" }),
  });
  if (blocked.response.status !== 403) fail(`external register expected 403, got ${blocked.response.status}: ${blocked.text}`);

  const corporateEmail = `employee-${Date.now()}@lukoil.com`;
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();

  await page.goto("/", { waitUntil: "networkidle" });
  await page.getByText("Только для сотрудников компании").waitFor({ state: "visible" });
  await page.getByRole("button", { name: "Регистрация" }).click();
  await page.getByLabel("Имя").fill("Corporate User");
  await page.getByLabel("Email").fill(corporateEmail);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Создать доступ" }).click();
  await page.getByText("Подтвердите email").waitFor({ state: "visible", timeout: 8000 });
  const devCodeText = await page.locator(".auth-dev-code").innerText();
  const devCode = devCodeText.match(/\d{4,12}/)?.[0];
  if (!devCode) fail(`dev code not found in UI: ${devCodeText}`);
  await page.getByLabel("Код из письма").fill(devCode);
  await page.getByRole("button", { name: "Подтвердить и войти" }).click();
  await page.getByText("Операционный контур АЗС").waitFor({ state: "visible", timeout: 8000 });
  await page.getByRole("button", { name: "Контроль" }).click();
  await page.locator("h2", { hasText: "Контроль АЗС" }).waitFor({ state: "visible" });
  await page.getByRole("tab", { name: "Главная" }).click();
  await page.getByText("Пользователь: Corporate User").waitFor({ state: "visible" });
  await page.getByRole("button", { name: "Выйти" }).click();
  await page.getByRole("button", { name: "Вход" }).waitFor({ state: "visible", timeout: 8000 });
  const afterLogout = await page.evaluate(async () => (await fetch("/api/stations", { credentials: "include" })).status);
  if (afterLogout !== 401) fail(`after logout expected /api/stations 401, got ${afterLogout}`);
  await browser.close();

  const allowlistedEmail = `allow-${Date.now()}@partner.test`;
  sqlite("INSERT OR REPLACE INTO email_allowlist (email, note, created_at) VALUES (?, ?, strftime('%s','now'))", [
    allowlistedEmail,
    "e2e",
  ]);
  const allowlisted = await api("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail, password, name: "Allowlisted User" }),
  });
  if (allowlisted.response.status !== 201) fail(`allowlisted register expected 201, got ${allowlisted.response.status}: ${allowlisted.text}`);
  const allowlistedPayload = parseJson(allowlisted.text);
  if (!allowlistedPayload.verificationRequired || !allowlistedPayload.devCode) {
    fail(`allowlisted register expected verification code, got: ${allowlisted.text}`);
  }
  const verified = await api("/api/auth/verify-email", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail, code: allowlistedPayload.devCode }),
  });
  if (verified.response.status !== 200) fail(`allowlisted verify expected 200, got ${verified.response.status}: ${verified.text}`);

  const resetPassword = "NewPassword123";
  const resetRequest = await api("/api/auth/request-password-reset", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail }),
  });
  if (resetRequest.response.status !== 200) {
    fail(`password reset request expected 200, got ${resetRequest.response.status}: ${resetRequest.text}`);
  }
  const resetPayload = parseJson(resetRequest.text);
  if (!resetPayload.verificationRequired || !resetPayload.devCode) {
    fail(`password reset request expected dev code, got: ${resetRequest.text}`);
  }
  const reset = await api("/api/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail, code: resetPayload.devCode, password: resetPassword }),
  });
  if (reset.response.status !== 200) fail(`password reset expected 200, got ${reset.response.status}: ${reset.text}`);

  const oldPasswordLogin = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail, password }),
  });
  if (oldPasswordLogin.response.status !== 401) {
    fail(`old password login expected 401, got ${oldPasswordLogin.response.status}: ${oldPasswordLogin.text}`);
  }
  const newPasswordLogin = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: allowlistedEmail, password: resetPassword }),
  });
  if (newPasswordLogin.response.status !== 200) {
    fail(`new password login expected 200, got ${newPasswordLogin.response.status}: ${newPasswordLogin.text}`);
  }

  const spbEmail = `spb-${Date.now()}@spb.lukoil.com`;
  const spb = await api("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email: spbEmail, password, name: "SPB User" }),
  });
  if (spb.response.status !== 201) fail(`spb domain register expected 201, got ${spb.response.status}: ${spb.text}`);

  console.log(JSON.stringify({ ok: true, corporateEmail, externalEmail, allowlistedEmail, spbEmail }, null, 2));
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
