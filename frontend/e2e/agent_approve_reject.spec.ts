/**
 * E2E test — Agent propose → approve / reject paths.
 * Task 37.2.
 *
 * Verifies Property 8 (Read-Only Agent Boundary) at the UI layer:
 *  - propose → reject leaves Aurora unchanged
 *  - propose → approve produces a revision with change_source='agent'
 *
 * The test stubs the agent chat backend so it returns a deterministic
 * proposal, then drives the UI to approve / reject and asserts:
 *   * Reject flow: no `POST /assets` request is fired.
 *   * Approve flow: exactly one `POST /assets` request is fired with
 *     change_source='agent' in the body.
 *
 * Validates: R7.7, R23.5.
 */
import { test, expect, type Route } from "@playwright/test";

const PROPOSAL = {
  type: "create_asset",
  reasoning: "Detected an unregistered behavior recording in s3://aind-ephys/2026/05/01/run-001.json.",
  payload: {
    name: "agent-proposed-asset",
    storage_uri: "s3://aind-ephys/2026/05/01/run-001.json",
    data_type: "behavior",
  },
};

async function stubAuth(page: any) {
  // Inject a fake Cognito id_token so the auth check in App.tsx passes.
  await page.addInitScript(() => {
    const fakeJwt = (() => {
      const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
      const payload = btoa(JSON.stringify({
        sub: "playwright-user",
        email: "playwright@allen.test",
        exp: Math.floor(Date.now() / 1000) + 3600,
      }));
      return `${header}.${payload}.signature`;
    })();
    localStorage.setItem("biodata_registry_id_token", fakeJwt);
    localStorage.setItem(
      "biodata_registry_id_token_exp",
      String(Math.floor(Date.now() / 1000) + 3600),
    );
  });
}

test.describe("Agent approve/reject", () => {
  test("Reject flow leaves the registry unchanged", async ({ page }) => {
    await stubAuth(page);
    let postAssetsCount = 0;

    // Stub the agent chat endpoint to return a deterministic proposal.
    await page.route("**/agent/chat", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          reply: "I noticed an unregistered file. Would you like to register it?",
          proposal: PROPOSAL,
        }),
      });
    });

    // Track POST /assets calls — a reject must not fire one.
    await page.route("**/assets", async (route: Route) => {
      if (route.request().method() === "POST") {
        postAssetsCount += 1;
      }
      await route.continue();
    });

    await page.goto("/agent");
    await expect(page.locator("h2", { hasText: "Metadata Agent" })).toBeVisible({ timeout: 10_000 });

    await page.locator(".chat-input textarea").fill("Find unregistered files");
    await page.locator(".chat-input button[type='submit']").click();

    // Proposal card appears.
    await expect(page.locator("h3", { hasText: /Proposed change/ })).toBeVisible({ timeout: 15_000 });

    // Click Reject.
    await page.locator("button", { hasText: "Reject" }).click();
    await expect(page.locator("text=✗ Rejected").first()).toBeVisible();

    // Wait a moment to ensure no async write fires.
    await page.waitForTimeout(500);
    expect(postAssetsCount).toBe(0);
  });

  test("Approve flow writes change_source='agent'", async ({ page }) => {
    await stubAuth(page);
    let approveCallBody: any = null;

    await page.route("**/agent/chat", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          reply: "I noticed an unregistered file.",
          proposal: PROPOSAL,
        }),
      });
    });

    // Stub POST /assets and capture the body for assertions.
    await page.route("**/assets", async (route: Route) => {
      if (route.request().method() !== "POST") return route.continue();
      approveCallBody = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: "00000000-0000-0000-0000-deadbeef0001",
          name: PROPOSAL.payload.name,
          storage_uri: PROPOSAL.payload.storage_uri,
          data_type: PROPOSAL.payload.data_type,
        }),
      });
    });

    await page.goto("/agent");
    await expect(page.locator("h2", { hasText: "Metadata Agent" })).toBeVisible({ timeout: 10_000 });
    await page.locator(".chat-input textarea").fill("Find unregistered files");
    await page.locator(".chat-input button[type='submit']").click();

    await expect(page.locator("h3", { hasText: /Proposed change/ })).toBeVisible({ timeout: 15_000 });
    await page.locator("button", { hasText: "Approve" }).click();
    await expect(page.locator("text=✓ Approved").first()).toBeVisible({ timeout: 10_000 });

    expect(approveCallBody).not.toBeNull();
    expect(approveCallBody.change_source).toBe("agent");
    expect(approveCallBody.name).toBe(PROPOSAL.payload.name);
    expect(approveCallBody.storage_uri).toBe(PROPOSAL.payload.storage_uri);
  });
});
