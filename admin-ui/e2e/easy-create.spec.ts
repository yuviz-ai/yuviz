import { expect, request, test, type Page, type WebSocketRoute } from "@playwright/test";
import { easyCopy } from "../lib/easyCopy";

// Seeding is through the Config API as a superadmin. E2E_EMAIL and E2E_PASSWORD
// are required (no defaults); E2E_CONFIG_URL is optional.
// Each run creates two fresh accounts with a random suffix, so reruns never
// collide: A has one speech-recognition, one AI and one voice setup ({1,1,1}),
// B has two of each ({2,2,2}). Nothing else in the database is read or changed.
const CONFIG_URL = process.env.E2E_CONFIG_URL ?? "http://localhost:8010";
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
if (!EMAIL || !PASSWORD) throw new Error("Set E2E_EMAIL and E2E_PASSWORD to a Config superadmin login.");
const TOKEN_KEY = "yuviz_access_token";
const ACTIVE_TENANT_KEY = "yuviz.activeTenantId";

// Same list as BANNED in services/config/tests/test_agent_templates.py.
const BANNED = /\b(agent|prompt|llm|stt|tts|provider|workflow|configuration|engine|latency|model)s?\b/gi;

const VOICE_JOB = "Appointment booking";
const CHAT_JOB = "Questions and answers";
const CHAT_REPLY = "Sure, I can help with that.";

const suffix = Math.random().toString(36).slice(2, 8);
let token = "";
let tenantA = "";
let tenantB = "";
let tenantNoSpeech = "";
let tenantEmpty = "";
let tenantPreselect = "";
let tenantDocs = "";
const configIds: Record<string, string> = {};
let nameCounter = 0;
const uniqueName = (prefix: string) => `${prefix} ${suffix}${++nameCounter}`;

test.beforeAll(async () => {
  const api = await request.newContext({ baseURL: CONFIG_URL });
  const login = await api.post("/auth/login", { data: { email: EMAIL, password: PASSWORD } });
  expect(login.ok()).toBeTruthy();
  token = (await login.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };

  const seed = async (slug: string, count: number) => {
    const created = await api.post("/tenants", { headers, data: { name: `E2E ${slug}`, slug } });
    expect(created.ok()).toBeTruthy();
    const { id } = await created.json();
    const names = count === 1 ? ["Main"] : ["Front", "Back"];
    for (const n of names) {
      const configs = [
        { role: "llm", engine: "openai", model: "gpt-4o-mini", api_key: "sk-e2e-not-a-real-key", name: `${n} AI` },
        { role: "stt", engine: "whisper", model: "base", name: `${n} Ears` },
        { role: "tts", engine: "macos", voice: "Samantha", name: `${n} Voice` },
      ];
      for (const data of configs) {
        const res = await api.post(`/tenants/${id}/providers`, { headers, data });
        expect(res.ok()).toBeTruthy();
        configIds[`${slug}:${data.name}`] = (await res.json()).id;
      }
    }
    return slug;
  };
  tenantA = await seed(`e2e-a-${suffix}`, 1);
  tenantB = await seed(`e2e-b-${suffix}`, 2);

  // C has an AI service only (no speech recognition, no voice); D has nothing.
  const seedPartial = async (slug: string, withAi: boolean) => {
    const created = await api.post("/tenants", { headers, data: { name: `E2E ${slug}`, slug } });
    expect(created.ok()).toBeTruthy();
    const { id } = await created.json();
    if (withAi) {
      const ai = { role: "llm", engine: "openai", model: "gpt-4o-mini", api_key: "sk-e2e-not-a-real-key", name: "Main AI" };
      expect((await api.post(`/tenants/${id}/providers`, { headers, data: ai })).ok()).toBeTruthy();
    }
    return slug;
  };
  // E has two of each, plus one receptionist already using its second voice, so
  // Easy should preselect that voice (and only that).
  tenantPreselect = await seed(`e2e-e-${suffix}`, 2);
  const existing = await api.post(`/tenants/${tenantPreselect}/agents`, {
    headers,
    data: { slug: "existing", name: "Existing", tts_config_id: configIds[`${tenantPreselect}:Back Voice`] },
  });
  expect(existing.ok()).toBeTruthy();
  // F is A plus an embedding setup, which is what turns file uploads on.
  tenantDocs = await seed(`e2e-f-${suffix}`, 1);
  const docsTenantId = (await (await api.get(`/tenants/${tenantDocs}`, { headers })).json()).id;
  const embedding = {
    role: "embedding",
    engine: "openai",
    model: "text-embedding-3-small",
    api_key: "sk-e2e-not-a-real-key",
    name: "Main Search",
  };
  expect((await api.post(`/tenants/${docsTenantId}/providers`, { headers, data: embedding })).ok()).toBeTruthy();
  tenantNoSpeech = await seedPartial(`e2e-c-${suffix}`, true);
  tenantEmpty = await seedPartial(`e2e-d-${suffix}`, false);
  await api.dispose();
});

async function openEasy(page: Page, tenantSlug: string) {
  await page.addInitScript(
    ([t, slug, tokenKey, tenantKey]) => {
      localStorage.setItem(tokenKey, t);
      localStorage.setItem(tenantKey, slug);
    },
    [token, tenantSlug, TOKEN_KEY, ACTIVE_TENANT_KEY],
  );
  await page.goto("/agents/new");
  await page.locator("button[aria-pressed]:not([disabled])").first().waitFor();
}

// The Easy flow container: the parent of the step tabs. Everything the Easy
// flow renders is inside it.
const flow = (page: Page) => page.locator(".tabs").locator("xpath=..");

async function scan(page: Page, label: string) {
  const text = await flow(page).innerText();
  expect(text.length, `${label}: scanned text is empty`).toBeGreaterThan(0);
  expect(text, `${label}: scope is the Easy flow`).toContain(easyCopy.stepTitles[0]);
  expect(text.match(BANNED) ?? [], `${label}: banned words`).toEqual([]);
}

// Labels (or placeholders) of every enabled form field in the flow.
async function controls(page: Page): Promise<string[]> {
  const found = await flow(page)
    .locator("input, textarea, select")
    .evaluateAll((els) =>
      els
        .filter((e) => !(e as HTMLInputElement).disabled && (e as HTMLInputElement).type !== "hidden")
        .map((e) => {
          const input = e as HTMLInputElement;
          return (input.labels?.[0]?.childNodes[0]?.textContent ?? input.placeholder).trim();
        }),
    );
  return found.sort();
}

async function expectStep(page: Page, index: number) {
  await expect(page.locator(".tab.active")).toHaveText(`${index + 1}. ${easyCopy.stepTitles[index]}`);
}

const next = (page: Page) => page.getByRole("button", { name: easyCopy.continue, exact: true }).click();

async function pickJob(page: Page, label: string) {
  await page.locator("button[aria-pressed]", { hasText: label }).click();
}

async function fillBusiness(page: Page, name: string, business = "Acme Dental") {
  await page.getByLabel(easyCopy.nameLabel, { exact: true }).fill(name);
  await page.getByLabel(easyCopy.businessNameLabel).fill(business);
  await page.getByLabel(easyCopy.businessFactsLabel).fill("Open 9 to 5.");
}

// Job, business and speaking steps, ending on the Test step with the agent created.
async function createThrough(page: Page, job: string, name: string) {
  await pickJob(page, job);
  await next(page);
  await fillBusiness(page, name);
  await next(page);
  await expectStep(page, 2);
  await next(page);
  await expectStep(page, 3);
}

async function chatTurn(page: Page, text: string) {
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill(text);
  await page.getByRole("button", { name: easyCopy.send }).click();
}

test("the six titles appear in order, and the active step follows", async ({ page }) => {
  await openEasy(page, tenantA);
  const expected = [
    "1. What should my AI receptionist do?",
    "2. Tell it about my business",
    "3. Choose how it speaks",
    "4. Test it",
    "5. Fix anything that's wrong",
    "6. Put it to work",
  ];
  expect(easyCopy.stepTitles.map((t, i) => `${i + 1}. ${t}`)).toEqual(expected);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual(expected);
  await expectStep(page, 0);
  await pickJob(page, CHAT_JOB);
  await next(page);
  await expectStep(page, 1);
  await fillBusiness(page, uniqueName("Titles"));
  await next(page);
  await expectStep(page, 2);
  await next(page);
  await expectStep(page, 3);
  await chatTurn(page, "What are your hours?");
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);
  await expectStep(page, 4);
  await next(page);
  await expectStep(page, 5);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual(expected);
});

test("one of each setup: full chat journey, control set and banned-word scan on every step", async ({ page }) => {
  await openEasy(page, tenantA);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 1");
  await pickJob(page, CHAT_JOB);
  await page.getByText(easyCopy.whatItDoes).waitFor();
  await scan(page, "step 1 selected");
  await next(page);

  // Step 2, with both required-field errors.
  expect(await controls(page)).toEqual(
    [easyCopy.nameLabel, easyCopy.businessNameLabel, easyCopy.businessFactsLabel].sort(),
  );
  await next(page);
  await expect(page.getByText(easyCopy.nameRequired)).toBeVisible();
  await scan(page, "name required");
  await page.getByLabel(easyCopy.nameLabel, { exact: true }).fill(uniqueName("Journey"));
  await next(page);
  await expect(page.getByText(easyCopy.businessNameRequired)).toBeVisible();
  await scan(page, "business name required");
  await page.getByLabel(easyCopy.businessNameLabel).fill("Acme {{secret}}");
  await next(page);

  // Step 3: one setup of each, so Language is the only control. The server
  // refuses the double braces and Easy shows its own text.
  await expectStep(page, 2);
  expect(await controls(page)).toEqual([easyCopy.languageLabel]);
  await scan(page, "step 3");
  await next(page);
  await expect(page.getByText(easyCopy.doubleBraces)).toBeVisible();
  await scan(page, "double braces refused");
  await page.getByRole("button", { name: easyCopy.back }).click();
  await page.getByLabel(easyCopy.businessNameLabel).fill("Acme");
  await next(page);
  await next(page);

  // Step 4, chat variant.
  await expectStep(page, 3);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 4 before start");
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await expect(page.getByPlaceholder(easyCopy.chatPlaceholder)).toBeEnabled();
  expect(await controls(page)).toEqual([easyCopy.chatPlaceholder]);
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("What are your hours?");
  await page.getByRole("button", { name: easyCopy.send }).click();
  await page.getByText(CHAT_REPLY).waitFor();
  await scan(page, "step 4 after a turn");
  await next(page);

  // Step 5: Fix, Discard, Accept, Undo.
  await expectStep(page, 4);
  expect(await controls(page)).toEqual([easyCopy.problemLabel]);
  await scan(page, "step 5");
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await page.getByRole("button", { name: easyCopy.acceptFix }).waitFor();
  await expect(page.getByText(easyCopy.beforeLabel, { exact: true })).toBeVisible();
  await expect(page.getByText(easyCopy.afterLabel, { exact: true })).toBeVisible();
  expect(await controls(page)).toEqual([]);
  await scan(page, "fix proposal");
  await page.getByRole("button", { name: easyCopy.discardFix }).click();
  await expect(page.getByRole("button", { name: easyCopy.undoFix })).toHaveCount(0);
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await page.getByRole("button", { name: easyCopy.acceptFix }).click();
  await page.getByText(easyCopy.fixAccepted).waitFor();
  await scan(page, "fix accepted");
  await page.getByRole("button", { name: easyCopy.undoFix }).click();
  await page.getByText(easyCopy.fixUndone).waitFor();
  await scan(page, "fix undone");
  await next(page);

  // Step 6.
  await expectStep(page, 5);
  expect(await controls(page)).toEqual([]);
  await scan(page, "step 6");
  await page.getByRole("button", { name: easyCopy.putToWork, exact: true }).click();
  await page.getByText(easyCopy.liveTitle).waitFor();
  await expect(page.getByRole("link", { name: easyCopy.passedOnCallsLink })).toBeVisible();
  await scan(page, "live confirmation");
});

test("two of each setup: the dropdown set, and Continue waits for every choice", async ({ page }) => {
  await openEasy(page, tenantB);
  await pickJob(page, VOICE_JOB);
  await next(page);
  await fillBusiness(page, uniqueName("Pair"));
  await next(page);
  await expectStep(page, 2);

  expect(await controls(page)).toEqual(
    [easyCopy.languageLabel, easyCopy.aiServiceLabel, easyCopy.speechRecognitionLabel, easyCopy.voiceLabel].sort(),
  );
  await scan(page, "step 3 with choices");
  const optionsOf = (label: string) => page.getByLabel(label, { exact: true }).locator("option").allInnerTexts();
  expect(await optionsOf(easyCopy.aiServiceLabel)).toEqual([easyCopy.chooseOne, "Back AI", "Front AI"]);
  expect(await optionsOf(easyCopy.speechRecognitionLabel)).toEqual([easyCopy.chooseOne, "Back Ears", "Front Ears"]);
  expect(await optionsOf(easyCopy.voiceLabel)).toEqual([easyCopy.chooseOne, "Back Voice", "Front Voice"]);

  const cont = page.getByRole("button", { name: easyCopy.continue, exact: true });
  await expect(cont).toBeDisabled();
  await page.getByLabel(easyCopy.aiServiceLabel, { exact: true }).selectOption({ label: "Front AI" });
  await page.getByLabel(easyCopy.speechRecognitionLabel, { exact: true }).selectOption({ label: "Front Ears" });
  await expect(cont).toBeDisabled();
  await page.getByLabel(easyCopy.voiceLabel, { exact: true }).selectOption({ label: "Front Voice" });
  await expect(cont).toBeEnabled();
  await next(page);
  await expectStep(page, 3);
  await scan(page, "step 4 voice");
});

test("one-job chat test failures show only Easy text", async ({ page }) => {
  await openEasy(page, tenantA);
  await pickJob(page, CHAT_JOB);
  await next(page);
  await fillBusiness(page, uniqueName("Failures"));
  await next(page);
  await next(page);
  await expectStep(page, 3);

  // A mint failure, then a good start.
  let mintFails = 1;
  await page.route("**/test-sessions", (route) =>
    mintFails-- > 0 ? route.fulfill({ status: 502, json: { detail: "vendor said no" } }) : route.fallback(),
  );
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await expect(page.getByText(easyCopy.aiUnavailable)).toBeVisible();
  await scan(page, "mint 502");
  await page.getByRole("button", { name: easyCopy.startChatTest }).click();
  await page.getByPlaceholder(easyCopy.chatPlaceholder).waitFor();

  // Chat turns: 429 then 502, then a real turn so Fix has a session.
  const failures = [
    { status: 429, detail: "slow down", shows: easyCopy.tooManyRequests },
    { status: 502, detail: "upstream exploded", shows: easyCopy.aiUnavailable },
  ];
  let turn = 0;
  await page.route("**/test-chat", (route) =>
    turn < failures.length
      ? route.fulfill({ status: failures[turn].status, json: { detail: failures[turn].detail } })
      : route.fallback(),
  );
  for (const f of failures) {
    await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("Hello?");
    await page.getByRole("button", { name: easyCopy.send }).click();
    await expect(page.locator(".error-banner")).toHaveText(f.shows);
    expect(await page.locator(".error-banner").innerText()).not.toContain(f.detail);
    await scan(page, `chat ${f.status}`);
    turn++;
  }
  await page.getByPlaceholder(easyCopy.chatPlaceholder).fill("Hello?");
  await page.getByRole("button", { name: easyCopy.send }).click();
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);

  // Revise 502.
  await page.route("**/prompt/revise", (route) => route.fulfill({ status: 502, json: { detail: "ai_unavailable" } }));
  await page.getByLabel(easyCopy.problemLabel).fill("It repeated itself.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await expect(page.locator(".error-banner")).toHaveText(easyCopy.aiUnavailable);
  await scan(page, "revise 502");
});

test("an agent whose instructions were changed by hand shows the not-fixable message and no text box", async ({
  page,
}) => {
  await page.route("**/agents/from-template", async (route) => {
    const res = await route.fetch();
    await route.fulfill({ response: res, json: { ...(await res.json()), prompt_fixable: false } });
  });
  await openEasy(page, tenantA);
  await createThrough(page, CHAT_JOB, uniqueName("Hand edited"));
  await chatTurn(page, "What are your hours?");
  await page.getByText(CHAT_REPLY).waitFor();
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByText(easyCopy.notFixable)).toBeVisible();
  await expect(flow(page).locator("textarea")).toHaveCount(0);
  expect(await controls(page)).toEqual([]);
  await scan(page, "not fixable");
});

test("Advanced opens the existing wizard with its six titles", async ({ page }) => {
  await openEasy(page, tenantA);
  await page.getByRole("button", { name: easyCopy.advancedLink }).click();
  await expect(page.locator(".tabs .tab")).toHaveCount(6);
  expect(await page.locator(".tabs .tab").allInnerTexts()).toEqual([
    "1. Identity",
    "2. Language & Voice",
    "3. Limits",
    "4. Advanced",
    "5. Knowledge & Tools",
    "6. Review",
  ]);
});

test("voice test: every Start mints a fresh one-use credential, sent as the first frame", async ({ page }) => {
  const sockets: WebSocketRoute[] = [];
  const urls: string[] = [];
  const frames: string[][] = [];
  await page.routeWebSocket(/\/webcall/, (ws) => {
    const mine: string[] = [];
    sockets.push(ws);
    urls.push(ws.url());
    frames.push(mine);
    ws.onMessage((m) => mine.push(typeof m === "string" ? m : "<binary>"));
  });
  const minted: string[] = [];
  page.on("response", async (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/test-sessions") && res.ok()) {
      minted.push((await res.json()).credential);
    }
  });

  await openEasy(page, tenantA);
  await createThrough(page, VOICE_JOB, uniqueName("Voice"));
  await scan(page, "voice step 4");

  const startTalking = page.getByRole("button", { name: easyCopy.startTalking });
  let starts = 0;
  const start = async () => {
    await startTalking.click();
    starts++;
    await expect.poll(() => sockets.length).toBe(starts);
    await expect.poll(() => frames[starts - 1].length).toBeGreaterThan(0);
    await expect.poll(() => minted.length).toBe(starts);
  };
  const fixGatedOff = async () => {
    await next(page);
    await expectStep(page, 4);
    await expect(page.getByText(easyCopy.testFirst)).toBeVisible();
    await expect(flow(page).locator("textarea")).toHaveCount(0);
    await expect(page.getByRole("button", { name: easyCopy.suggestFix })).toHaveCount(0);
    await scan(page, "fix gated");
    await page.getByRole("button", { name: easyCopy.back }).click();
    await expectStep(page, 3);
  };

  // Start 1, then Fix is gated and Back returns to Test.
  await start();
  await fixGatedOff();

  // Stop, then Start.
  await start();
  await page.getByRole("button", { name: easyCopy.stop }).click();
  await expect(startTalking).toBeVisible();
  await start();

  // Forced reconnect: the server drops the socket, and the user starts again.
  await sockets[starts - 1].close();
  await expect(page.getByText(easyCopy.statusEnded)).toBeVisible();
  await scan(page, "call ended");
  await start();

  // A spoken line without the session id keeps Fix gated.
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "Hello, how can I help?" }));
  await expect(page.getByText("Hello, how can I help?")).toBeVisible();
  await scan(page, "transcript");
  await fixGatedOff();

  // A second call must not inherit the first call's transcript: Fix stays
  // gated until the new session's own first line.
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session-1" }));
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "First call line." }));
  await expect(page.getByText("First call line.")).toBeVisible();
  await page.getByRole("button", { name: easyCopy.stop }).click();
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session-2" }));
  await expect(page.getByText("First call line.")).toHaveCount(0);
  await fixGatedOff();

  // The session id arrives: Fix opens.
  await start();
  sockets[starts - 1].send(JSON.stringify({ type: "service_ready", session_id: "e2e-session" }));
  sockets[starts - 1].send(JSON.stringify({ type: "tts_result", text: "Hello again." }));
  await expect(page.getByText("Hello again.")).toBeVisible();
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByLabel(easyCopy.problemLabel)).toBeVisible();
  await page.route("**/prompt/revise", (route) => route.fulfill({ status: 502, json: { detail: "ai_unavailable" } }));
  await page.getByLabel(easyCopy.problemLabel).fill("It was slow to answer.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await expect(page.locator(".error-banner")).toHaveText(easyCopy.aiUnavailable);
  await scan(page, "voice revise 502");

  // Every Start, one mint, all different, none in a URL.
  expect(starts).toBe(7);
  expect(minted).toHaveLength(starts);
  expect(new Set(minted).size).toBe(starts);
  for (let i = 0; i < starts; i++) {
    expect(urls[i]).toMatch(/\/webcall\?tenant=.+&agent=.+&test=1$/);
    for (const c of minted) expect(urls[i]).not.toContain(c);
    const first = JSON.parse(frames[i][0]);
    expect(first.type).toBe("test_credential");
    expect(first.credential).toBe(minted[i]);
  }
});

test("a tenant missing a setup sees the plain message and link, and no create request is sent", async ({ page }) => {
  const creates: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/agents")) creates.push(r.url());
  });

  // Only an AI service: the chat job is open, every phone job is closed with the
  // speech-recognition message.
  await page.addInitScript(
    ([t, slug, tokenKey, tenantKey]) => {
      localStorage.setItem(tokenKey, t);
      localStorage.setItem(tenantKey, slug);
    },
    [token, tenantNoSpeech, TOKEN_KEY, ACTIVE_TENANT_KEY],
  );
  await page.goto("/agents/new");
  await page.locator("button[aria-pressed]:not([disabled])").first().waitFor();
  await expect(page.locator("button[aria-pressed]", { hasText: CHAT_JOB })).toBeEnabled();
  const voiceJob = page.locator("button[aria-pressed]", { hasText: VOICE_JOB });
  await expect(voiceJob).toBeDisabled();
  await expect(page.getByText(easyCopy.setUpSpeechRecognition).first()).toBeVisible();
  await expect(page.getByRole("link", { name: easyCopy.addItLink }).first()).toHaveAttribute("href", "/ai-voice");
  await scan(page, "missing speech recognition");

  // Nothing at all: every job is closed and the AI-service message shows.
  const other = await page.context().newPage();
  await other.addInitScript(
    ([t, slug, tokenKey, tenantKey]) => {
      localStorage.setItem(tokenKey, t);
      localStorage.setItem(tenantKey, slug);
    },
    [token, tenantEmpty, TOKEN_KEY, ACTIVE_TENANT_KEY],
  );
  other.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/agents")) creates.push(r.url());
  });
  await other.goto("/agents/new");
  await expect(other.getByText(easyCopy.connectAiService).first()).toBeVisible();
  await expect(other.locator("button[aria-pressed]")).not.toHaveCount(0);
  await expect(other.locator("button[aria-pressed]:not([disabled])")).toHaveCount(0);
  await scan(other, "missing AI service");
  expect(creates).toEqual([]);
});

test("business facts over 1,000 characters show an inline error and send no create request", async ({ page }) => {
  const creates: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/agents")) creates.push(r.url());
  });
  await openEasy(page, tenantA);
  await pickJob(page, CHAT_JOB);
  await next(page);
  await page.getByLabel(easyCopy.nameLabel, { exact: true }).fill(uniqueName("Long facts"));
  await page.getByLabel(easyCopy.businessNameLabel).fill("Acme");
  await page.getByLabel(easyCopy.businessFactsLabel).fill("x".repeat(1001));
  await next(page);
  await expect(page.getByText(easyCopy.factsTooLong)).toBeVisible();
  await expectStep(page, 1);
  expect(creates).toEqual([]);
});

test("the voice dropdown preselects the voice of the newest existing receptionist", async ({ page }) => {
  await openEasy(page, tenantPreselect);
  await pickJob(page, VOICE_JOB);
  await next(page);
  await fillBusiness(page, uniqueName("Preselect"));
  await next(page);
  await expectStep(page, 2);
  const voice = page.getByLabel(easyCopy.voiceLabel, { exact: true });
  await expect(voice).toHaveValue(configIds[`${tenantPreselect}:Back Voice`]);
  // Only the voice is carried over: the other two choices stay open.
  await expect(page.getByLabel(easyCopy.aiServiceLabel, { exact: true })).toHaveValue("");
  await expect(page.getByLabel(easyCopy.speechRecognitionLabel, { exact: true })).toHaveValue("");
  await expect(page.getByRole("button", { name: easyCopy.continue, exact: true })).toBeDisabled();
});

test("Stop, Start, then Back: a fresh mint each time, Fix waits for the new session, Back creates nothing", async ({ page }) => {
  const sockets: WebSocketRoute[] = [];
  const frames: string[][] = [];
  await page.routeWebSocket(/\/webcall/, (ws) => {
    const mine: string[] = [];
    sockets.push(ws);
    frames.push(mine);
    ws.onMessage((m) => mine.push(typeof m === "string" ? m : "<binary>"));
  });
  const minted: string[] = [];
  const creates: string[] = [];
  page.on("response", async (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/test-sessions") && res.ok()) {
      minted.push((await res.json()).credential);
    }
  });
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/agents(\/from-template)?$/.test(r.url())) creates.push(r.url());
  });

  await openEasy(page, tenantA);
  await createThrough(page, VOICE_JOB, uniqueName("Back"));
  const createdBefore = creates.length;
  expect(createdBefore).toBe(1);

  // Start 1 with a full session, Stop, then Start 2: no session yet.
  const startTalking = page.getByRole("button", { name: easyCopy.startTalking });
  await startTalking.click();
  await expect.poll(() => sockets.length).toBe(1);
  sockets[0].send(JSON.stringify({ type: "service_ready", session_id: "back-session-1" }));
  sockets[0].send(JSON.stringify({ type: "tts_result", text: "First line." }));
  await expect(page.getByText("First line.")).toBeVisible();
  await page.getByRole("button", { name: easyCopy.stop }).click();
  await expect(startTalking).toBeVisible();
  await startTalking.click();
  await expect.poll(() => sockets.length).toBe(2);
  await expect.poll(() => frames[1].length).toBeGreaterThan(0);
  expect(minted).toHaveLength(2);
  expect(new Set(minted).size).toBe(2);
  expect(JSON.parse(frames[1][0]).credential).toBe(minted[1]);

  // Fix stays shut for the second session, whatever the first one said.
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByText(easyCopy.testFirst)).toBeVisible();
  await expect(flow(page).locator("textarea")).toHaveCount(0);

  // Back returns to Test and creates nothing.
  await page.getByRole("button", { name: easyCopy.back }).click();
  await expectStep(page, 3);
  expect(creates).toHaveLength(createdBefore);

  // Coming back leaves a dead call, so Start mints again; that session's id and a line open Fix.
  await startTalking.click();
  await expect.poll(() => sockets.length).toBe(3);
  expect(minted).toHaveLength(3);
  expect(new Set(minted).size).toBe(3);
  sockets[2].send(JSON.stringify({ type: "service_ready", session_id: "back-session-3" }));
  sockets[2].send(JSON.stringify({ type: "tts_result", text: "Second line." }));
  await expect(page.getByText("Second line.")).toBeVisible();
  await next(page);
  await expectStep(page, 4);
  await expect(page.getByLabel(easyCopy.problemLabel)).toBeVisible();

  // With a proposal on screen, Back is gone.
  await page.route("**/prompt/revise", (route) =>
    route.fulfill({ status: 200, json: { before: "Old text.", after: "New text.", base_prompt_sha256: "x" } }),
  );
  await expect(page.getByRole("button", { name: easyCopy.back })).toHaveCount(1);
  await page.getByLabel(easyCopy.problemLabel).fill("It was slow to answer.");
  await page.getByRole("button", { name: easyCopy.suggestFix }).click();
  await page.getByRole("button", { name: easyCopy.acceptFix }).waitFor();
  await expect(page.getByRole("button", { name: easyCopy.back })).toHaveCount(0);
  expect(creates).toHaveLength(createdBefore);
});

// ---- documents the receptionist can look up --------------------------------------------------

const KNOWLEDGE_URL = process.env.E2E_KNOWLEDGE_URL ?? "http://localhost:8110";
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "*",
};

// Stands in for the knowledge service. `calls` records "METHOD path" in order;
// `failAttach` makes that many attach calls fail before they succeed.
async function stubKnowledge(page: Page, existing: { id: string; name: string }[], failAttach = 0) {
  const calls: string[] = [];
  const bodies: Record<string, unknown>[] = [];
  let attachFailures = failAttach;
  await page.route(`${KNOWLEDGE_URL}/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(req.url()).pathname;
    const reply = (status: number, json: unknown) => route.fulfill({ status, headers: CORS, json });
    if (req.method() === "GET" && path.endsWith("/knowledge-bases")) {
      return reply(200, existing.map((k) => ({ ...k, slug: k.id, description: "", status: "active" })));
    }
    calls.push(`${req.method()} ${path.replace(/\/(tenants|agents)\/[^/]+/, "/$1/<id>").replace(/\/knowledge-bases\/[^/]+/, "/knowledge-bases/<id>")}`);
    if (req.method() === "POST" && /^\/tenants\/[^/]+\/knowledge-bases$/.test(path)) {
      bodies.push(req.postDataJSON());
      return reply(201, { id: "kb-new", name: req.postDataJSON().name, slug: req.postDataJSON().slug });
    }
    if (req.method() === "POST" && path.endsWith("/documents")) return reply(201, { id: "doc-1" });
    if (req.method() === "POST" && /\/agents\/[^/]+\/knowledge-bases$/.test(path)) {
      bodies.push(req.postDataJSON());
      if (attachFailures-- > 0) return reply(500, { detail: "vector store exploded" });
      return reply(201, { ...req.postDataJSON(), agent_id: "a" });
    }
    return reply(404, { detail: "unexpected" });
  });
  return { calls, bodies };
}

const textFile = (name: string) => ({ name, mimeType: "text/plain", buffer: Buffer.from("We open at nine.") });

async function toBusinessStep(page: Page, name: string, business = "Acme Dental") {
  await pickJob(page, CHAT_JOB);
  await next(page);
  await fillBusiness(page, name, business);
}

test("a collection the account already has can be ticked, and is attached after the receptionist is created", async ({
  page,
}) => {
  const stub = await stubKnowledge(page, [
    { id: "kb-hours", name: "Opening hours" },
    { id: "kb-menu", name: "Price list" },
  ]);
  await openEasy(page, tenantA);
  await toBusinessStep(page, uniqueName("Tick"));
  await expect(page.getByText(easyCopy.documentsLabel)).toBeVisible();
  await scan(page, "documents section");
  await page.getByLabel("Price list").check();
  await next(page);
  await next(page);
  await expectStep(page, 3);
  await expect.poll(() => stub.calls).toEqual(["POST /agents/<id>/knowledge-bases"]);
  expect(stub.bodies).toEqual([{ kb_id: "kb-menu", enabled: true }]);
  await next(page);
  await next(page);
  await expectStep(page, 5);
  await expect(page.getByText(`${easyCopy.documentsAttachedLabel}: 1 (Price list)`)).toBeVisible();
  await scan(page, "documents attached summary");
});

test("without document search set up, uploads are replaced by a plain line and a link", async ({ page }) => {
  await stubKnowledge(page, []);
  await openEasy(page, tenantA);
  await toBusinessStep(page, uniqueName("NoSearch"));
  await expect(page.getByText(easyCopy.uploadNeedsSetup)).toBeVisible();
  await expect(page.getByRole("link", { name: easyCopy.uploadNeedsSetupLink })).toHaveAttribute("href", "/knowledge-bases");
  await expect(page.getByLabel(easyCopy.uploadFilesLabel)).toHaveCount(0);
  await scan(page, "uploads unavailable");
});

test("uploaded files create a collection, are uploaded one by one, then attached, in that order", async ({ page }) => {
  const stub = await stubKnowledge(page, []);
  await openEasy(page, tenantDocs);
  await toBusinessStep(page, uniqueName("Upload"), "Acme Dental");
  await page.getByLabel(easyCopy.uploadFilesLabel).setInputFiles([textFile("hours.txt"), textFile("prices.md")]);
  await expect(page.getByRole("list", { name: easyCopy.filesChosenLabel }).getByRole("listitem")).toHaveCount(2);
  await scan(page, "files chosen");
  await next(page);
  await next(page);
  await expectStep(page, 3);
  await expect
    .poll(() => stub.calls)
    .toEqual([
      "POST /tenants/<id>/knowledge-bases",
      "POST /knowledge-bases/<id>/documents",
      "POST /knowledge-bases/<id>/documents",
      "POST /agents/<id>/knowledge-bases",
    ]);
  const [created, attach] = stub.bodies;
  expect(created.name).toBe("Acme Dental documents");
  expect(created.slug).toMatch(/^acme-dental-documents-[a-z0-9]{1,6}$/);
  expect(created.embedding_config_id).toEqual(expect.any(String));
  expect(attach).toEqual({ kb_id: "kb-new", enabled: true });
  await next(page);
  await next(page);
  await expect(page.getByText(`${easyCopy.documentsAttachedLabel}: 1 (Acme Dental documents)`)).toBeVisible();
});

test("a file that is not .txt or .md shows a plain message and nothing is uploaded", async ({ page }) => {
  const stub = await stubKnowledge(page, []);
  await openEasy(page, tenantDocs);
  await toBusinessStep(page, uniqueName("Wrong"));
  await page
    .getByLabel(easyCopy.uploadFilesLabel)
    .setInputFiles({ name: "brochure.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF") });
  await expect(page.getByText(easyCopy.wrongFileType)).toBeVisible();
  await expect(page.getByRole("list", { name: easyCopy.filesChosenLabel })).toHaveCount(0);
  await scan(page, "wrong file type");
  await next(page);
  await next(page);
  await expectStep(page, 3);
  expect(stub.calls).toEqual([]);
});

test("a failed attach keeps the receptionist, shows a plain warning, and Try again retries only that", async ({
  page,
}) => {
  const stub = await stubKnowledge(page, [{ id: "kb-hours", name: "Opening hours" }], 1);
  await openEasy(page, tenantA);
  await toBusinessStep(page, uniqueName("Retry"));
  await page.getByLabel("Opening hours").check();
  await next(page);
  await next(page);
  await expectStep(page, 3);
  await expect(page.getByText(easyCopy.documentsWarning)).toBeVisible();
  expect(await flow(page).innerText()).not.toContain("vector store exploded");
  await scan(page, "documents warning");
  expect(stub.calls).toEqual(["POST /agents/<id>/knowledge-bases"]);

  await page.getByRole("button", { name: easyCopy.documentsTryAgain }).click();
  await expect(page.getByText(easyCopy.documentsWarning)).toHaveCount(0);
  expect(stub.calls).toEqual(["POST /agents/<id>/knowledge-bases", "POST /agents/<id>/knowledge-bases"]);
  await next(page);
  await next(page);
  await expect(page.getByText(`${easyCopy.documentsAttachedLabel}: 1 (Opening hours)`)).toBeVisible();
});
