import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";


export function computeIntegrity(content) {
  return `sha512-${createHash("sha512").update(content).digest("base64")}`;
}

export function publicationDecision(localIntegrity, registryIntegrity) {
  if (registryIntegrity === null) {
    return "publish";
  }
  if (registryIntegrity === localIntegrity) {
    return "skip";
  }
  throw new Error(
    `npm registry integrity mismatch: local ${localIntegrity}, registry ${registryIntegrity}`,
  );
}

function npm(args, options = {}) {
  const command = process.platform === "win32" ? "npm.cmd" : "npm";
  return spawnSync(command, args, {
    encoding: "utf8",
    shell: process.platform === "win32",
    windowsHide: true,
    ...options,
  });
}

function registryIntegrity(packageSpec) {
  const result = npm(["view", packageSpec, "dist.integrity", "--json"]);
  if (result.error) {
    throw result.error;
  }
  if (result.status === 0) {
    const value = JSON.parse(result.stdout);
    if (typeof value !== "string" || !value.startsWith("sha512-")) {
      throw new Error(`npm registry returned invalid integrity for ${packageSpec}`);
    }
    return value;
  }
  const errorText = `${result.stdout || ""}\n${result.stderr || ""}`;
  if (/\bE404\b|404 Not Found/i.test(errorText)) {
    return null;
  }
  throw new Error(`npm registry lookup failed for ${packageSpec}: ${errorText.trim()}`);
}

export function publishTarball(tarball) {
  const resolved = path.resolve(tarball);
  const match = /^agentpermit-(\d+\.\d+\.\d+)\.tgz$/.exec(path.basename(resolved));
  assert.ok(match, `unexpected npm tarball filename: ${path.basename(resolved)}`);
  const version = match[1];
  const releaseTag = process.env.RELEASE_TAG;
  if (releaseTag && releaseTag !== `v${version}`) {
    throw new Error(`release tag ${releaseTag} does not match npm tarball version ${version}`);
  }
  const localIntegrity = computeIntegrity(readFileSync(resolved));
  const decision = publicationDecision(
    localIntegrity,
    registryIntegrity(`agentpermit@${version}`),
  );
  if (decision === "skip") {
    console.log(`agentpermit@${version} already exists with matching integrity; skipping publish`);
    return;
  }
  const result = npm(["publish", resolved, "--access", "public", "--provenance"], {
    stdio: "inherit",
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`npm publish failed with exit code ${result.status}`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  assert.ok(process.argv[2], "usage: node publish_npm.mjs <npm-tarball>");
  publishTarball(process.argv[2]);
}
