import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const tarball = process.argv[2];
assert.ok(tarball, "usage: node scripts/smoke_npm_artifact.mjs <npm-tarball>");

const root = mkdtempSync(path.join(os.tmpdir(), "agentpermit-npm-smoke-"));
const home = path.join(root, "home");
const source = path.join(root, "source");
mkdirSync(home);
mkdirSync(source);

try {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const install = spawnSync(
    npm,
    ["install", "--ignore-scripts", "--no-audit", "--no-fund", path.resolve(tarball)],
    {
      cwd: root,
      encoding: "utf8",
      windowsHide: true,
      shell: process.platform === "win32",
      timeout: 60_000,
    },
  );
  assert.ifError(install.error);
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const npx = process.platform === "win32" ? "npx.cmd" : "npx";
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: {} },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
  ];
  const smoke = spawnSync(
    npx,
    ["--no-install", "agentpermit", "--home", home, "mcp", "--source", source, "--task", "artifact-smoke"],
    {
      encoding: "utf8",
      input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
      timeout: 30_000,
      windowsHide: true,
      shell: process.platform === "win32",
    },
  );
  assert.ifError(smoke.error);
  assert.equal(smoke.status, 0, smoke.stderr || smoke.stdout);
  const responses = smoke.stdout
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  assert.equal(responses.length, 2, smoke.stdout);
  assert.equal(responses[0].id, 1);
  assert.equal(responses[0].result.serverInfo.name, "agentpermit");
  assert.equal(responses[1].id, 2);
  assert.ok(responses[1].result.tools.length > 0);
  console.log("clean npm artifact MCP smoke passed");
} finally {
  rmSync(root, { recursive: true, force: true });
}
