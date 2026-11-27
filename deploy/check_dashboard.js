// Pre-deploy check: sign in to a local copy of the dashboard in a phone-sized headless browser and make sure
// every tab renders something (not a blank page) and no script error occurs. Run by deploy/redeploy.sh.
// Usage: node deploy/check_dashboard.js http://127.0.0.1:PORT USER PASSWORD
const { chromium, devices } = require("playwright");

(async () => {
  const [base, user, pw] = process.argv.slice(2);
  const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  const problems = [];
  try {
    const ctx = await browser.newContext({ ...devices["iPhone 13"] });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => problems.push("script error: " + e.message));
    page.on("response", (r) => { if (r.url().includes("/api/") && !r.url().endsWith("/api/health") && r.status() >= 500) problems.push(`HTTP ${r.status()} ${r.url()}`); });
    await page.goto(base + "/");
    await page.fill("#u", user);
    await page.fill("#p", pw);
    await page.click("button[type=submit]");
    await page.waitForSelector("#app:not(.hidden)", { timeout: 15000 });
    const tabs = await page.$$eval("#tabs button", (bs) => bs.map((b) => b.dataset.tab));
    for (const tab of tabs) {
      await page.click(`#tabs button[data-tab="${tab}"]`);
      try {
        await page.waitForFunction((t) => document.querySelector("#" + t).textContent.trim().length > 20, tab, { timeout: 10000 });
      } catch (e) { problems.push(`tab "${tab}" stayed blank`); }
      const err = await page.$(`#${tab} h2`);
      if (err && (await err.textContent()).includes("could not load")) problems.push(`tab "${tab}" shows a load error`);
    }
  } catch (e) {
    problems.push("check could not finish: " + e.message);
  } finally {
    await browser.close();
  }
  if (problems.length) {
    for (const p of problems) console.log("FAIL", p);
    console.log("dashboard_render_check: BROKEN");
    process.exit(1);
  }
  console.log("dashboard_render_check: OK");
})();
