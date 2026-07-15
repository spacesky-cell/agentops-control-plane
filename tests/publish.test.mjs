import assert from "node:assert/strict";
import test from "node:test";

import { computeIntegrity, publicationDecision } from "../scripts/publish_npm.mjs";


test("computes npm-compatible sha512 integrity", () => {
  assert.equal(
    computeIntegrity(Buffer.from("agentpermit")),
    "sha512-vIPCXuAETJINnONjVAuTYy2NX39l/R4OQNfUFjljvl414sgtJECa+vX24iFdnrxnipUT6JFT2aB4dYdr0GbJeg==",
  );
});

test("publishes when the exact version is missing", () => {
  assert.equal(publicationDecision("sha512-local", null), "publish");
});

test("skips only when registry integrity exactly matches", () => {
  assert.equal(publicationDecision("sha512-local", "sha512-local"), "skip");
  assert.throws(
    () => publicationDecision("sha512-local", "sha512-other"),
    /integrity mismatch/,
  );
});
