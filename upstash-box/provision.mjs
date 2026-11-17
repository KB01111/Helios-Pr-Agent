// Provision (or reuse) an Upstash Box that auto-reviews PRs on a cron schedule.
// All secrets come from env vars only — never commit them.
//
//   UPSTASH_BOX_API_KEY=box_...  (required)
//   GITHUB_TOKEN=...             (required, needs repo + pull-request read/write)
//   OPENROUTER_API_KEY=...       (required)
//   REPOS="owner/repo,owner/repo2" (required)
//   BOX_NAME="pr-agent-runner"   MODEL="thinkingmachines/inkling-2-small:free"
//   PR_CRON="*/15 * * * *"      (5-field UTC cron; default every 15 min)
//   BOX_SIZE="small"
//
// Usage: node provision.mjs [--status]
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Box } from "@upstash/box";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODE_STATUS = process.argv.includes("--status");
const BOX_NAME = process.env.BOX_NAME || "pr-agent-runner";
const MODEL = process.env.MODEL || "thinkingmachines/inkling-2-small:free";
const PR_CRON = process.env.PR_CRON || "*/15 * * * *";
const BOX_SIZE = process.env.BOX_SIZE || "small";
const RUN_CMD = "python3 /workspace/home/auto-review.py >> /workspace/home/auto-review.log 2>&1";

function need(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`missing env: ${name}`);
    process.exit(1);
  }
  return v;
}

const apiKey = need("UPSTASH_BOX_API_KEY");

async function getBox() {
  try {
    return await Box.getByName(BOX_NAME, { apiKey });
  } catch {
    return null;
  }
}

if (MODE_STATUS) {
  const box = await getBox();
  if (!box) {
    console.log(JSON.stringify({ exists: false, name: BOX_NAME }));
    process.exit(0);
  }
  const status = await box.getStatus().catch((e) => ({ status: "error: " + String(e) }));
  const schedules = await box.schedule.list().catch(() => []);
  console.log(JSON.stringify({ exists: true, id: box.id, name: BOX_NAME, status, schedules }, null, 2));
  process.exit(0);
}

need("GITHUB_TOKEN");
need("OPENROUTER_API_KEY");
const repos = need("REPOS");

let box = await getBox();
if (!box) {
  box = await Box.create({
    apiKey,
    runtime: "python",
    name: BOX_NAME,
    size: BOX_SIZE,
    env: {
      GITHUB_TOKEN: process.env.GITHUB_TOKEN,
      OPENROUTER_API_KEY: process.env.OPENROUTER_API_KEY,
      REPOS: repos,
      MODEL,
    },
    initCommand: RUN_CMD,
  });
  console.log(`created box ${box.id}`);
} else {
  console.log(`reusing box ${box.id}`);
}

// Upload the review loop (also refresh secrets file for reused boxes).
const script = readFileSync(join(HERE, "auto-review.py"), "utf8");
await box.files.write({ path: "auto-review.py", content: script });
await box.files.write({
  path: ".box-env",
  content: [
    `GITHUB_TOKEN=${process.env.GITHUB_TOKEN}`,
    `OPENROUTER_API_KEY=${process.env.OPENROUTER_API_KEY}`,
    `REPOS=${repos}`,
    `MODEL=${MODEL}`,
  ].join("\n") + "\n",
});

// Replace any previous auto-review schedule with the new cron.
const schedules = await box.schedule.list().catch(() => []);
for (const s of schedules) {
  const cmd = (s.command || []).join(" ");
  if (s.status !== "deleted" && cmd.includes("auto-review.py")) {
    await box.schedule.delete(s.id).catch(() => {});
    console.log(`deleted old schedule ${s.id}`);
  }
}
const schedule = await box.schedule.exec({
  cron: PR_CRON,
  command: ["bash", "-c", RUN_CMD],
  folder: "/workspace/home",
});
console.log(JSON.stringify({ box_id: box.id, schedule_id: schedule.id, cron: PR_CRON, model: MODEL }, null, 2));
