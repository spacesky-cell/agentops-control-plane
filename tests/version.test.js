const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");

test("npm, Python project, and runtime versions stay synchronized", () => {
  const packageJsonPath = path.join(root, "package.json");
  const runtimePath = path.join(root, "agentpermit", "__init__.py");
  assert.ok(fs.existsSync(packageJsonPath), "package.json must exist");
  assert.ok(fs.existsSync(runtimePath), "agentpermit/__init__.py must exist");

  const npmVersion = JSON.parse(fs.readFileSync(packageJsonPath, "utf8")).version;
  const pyproject = fs.readFileSync(path.join(root, "pyproject.toml"), "utf8");
  const runtime = fs.readFileSync(runtimePath, "utf8");
  const projectMatch = pyproject.match(/^version\s*=\s*"([^"]+)"/m);
  const runtimeMatch = runtime.match(/^__version__\s*=\s*"([^"]+)"/m);

  assert.ok(projectMatch, "pyproject.toml must declare project.version");
  assert.ok(runtimeMatch, "agentpermit.__version__ must be declared");
  assert.equal(npmVersion, projectMatch[1]);
  assert.equal(npmVersion, runtimeMatch[1]);
});

test("npm manifest has no dependencies or install hooks and ships only runtime sources", () => {
  const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));

  assert.equal(packageJson.dependencies, undefined);
  assert.equal(packageJson.scripts.postinstall, undefined);
  assert.deepEqual(packageJson.files, [
    "bin/agentpermit.js",
    "agentpermit/*.py",
    "README.md",
    "LICENSE",
  ]);
});
