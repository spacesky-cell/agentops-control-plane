const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const { EventEmitter } = require("node:events");
const { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const launcherPath = path.resolve(__dirname, "..", "bin", "agentpermit.js");

function loadLauncher() {
  assert.ok(existsSync(launcherPath), "bin/agentpermit.js must exist");
  delete require.cache[launcherPath];
  return require(launcherPath);
}

function versionProbe(versions, calls) {
  return (command, args, options) => {
    calls.push({ command, args, options });
    const result = versions[command];
    if (result instanceof Error) {
      return { error: result };
    }
    if (result === undefined) {
      return { error: new Error(`${command} not found`) };
    }
    return { status: 0, stdout: `${result}\n`, stderr: "" };
  };
}

test("AGENTPERMIT_PYTHON takes precedence over platform defaults", () => {
  const { findPython } = loadLauncher();
  const calls = [];

  const selected = findPython({
    env: { AGENTPERMIT_PYTHON: "C:\\Python311\\python.exe" },
    platform: "win32",
    spawnSync: versionProbe({ "C:\\Python311\\python.exe": "3.11.9", py: "3.12.4" }, calls),
  });

  assert.deepEqual(selected, { command: "C:\\Python311\\python.exe", prefixArgs: [] });
  assert.deepEqual(calls.map(({ command }) => command), ["C:\\Python311\\python.exe"]);
});

test("Windows uses py -3 before python3 and python", () => {
  const { findPython } = loadLauncher();
  const calls = [];

  const selected = findPython({
    env: {},
    platform: "win32",
    spawnSync: versionProbe({ py: "3.10.0", python3: "3.12.0", python: "3.13.0" }, calls),
  });

  assert.deepEqual(selected, { command: "py", prefixArgs: ["-3"] });
  assert.deepEqual(calls.map(({ command, args }) => ({ command, args: args.slice(0, 3) })), [
    { command: "py", args: ["-3", "-I", "-c"] },
  ]);
});

test("Python versions below 3.10 are rejected in favor of the next candidate", () => {
  const { findPython } = loadLauncher();
  const calls = [];

  const selected = findPython({
    env: { AGENTPERMIT_PYTHON: "old-python" },
    platform: "linux",
    spawnSync: versionProbe({ "old-python": "3.9.19", python3: "3.10.0" }, calls),
  });

  assert.deepEqual(selected, { command: "python3", prefixArgs: [] });
  assert.deepEqual(calls.map(({ command }) => command), ["old-python", "python3"]);
});

test("launcher uses an isolated bootstrap, inherited stdio, and child exit status", () => {
  const { runLauncher } = loadLauncher();
  const child = new EventEmitter();
  const spawnCalls = [];
  const exitCodes = [];
  const packageRoot = path.resolve(__dirname, "..");
  const env = { AGENTPERMIT_PYTHON: "python-ok", PYTHONPATH: "hostile-path" };

  const started = runLauncher({
    argv: ["--home", "demo", "runs"],
    env,
    platform: "linux",
    packageRoot,
    spawnSync: versionProbe({ "python-ok": "3.12.2" }, []),
    spawn(command, args, options) {
      spawnCalls.push({ command, args, options });
      return child;
    },
    onExit(code) {
      exitCodes.push(code);
    },
  });
  child.emit("exit", 23, null);

  assert.equal(started, child);
  assert.equal(spawnCalls.length, 1);
  const call = spawnCalls[0];
  assert.equal(call.command, "python-ok");
  assert.deepEqual(call.args.slice(0, 2), ["-I", "-c"]);
  assert.match(call.args[2], /runpy\.run_module\('agentpermit'/);
  assert.deepEqual(call.args.slice(3), [packageRoot, "--home", "demo", "runs"]);
  assert.deepEqual(call.options, { env, stdio: "inherit", detached: true });
  assert.deepEqual(exitCodes, [23]);
});

test("isolated bootstrap cannot be hijacked by an agentpermit package in the working directory", () => {
  const maliciousRoot = mkdtempSync(path.join(os.tmpdir(), "agentpermit-hijack-"));
  const maliciousPackage = path.join(maliciousRoot, "agentpermit");
  const marker = path.join(maliciousRoot, "hijacked.txt");
  mkdirSync(maliciousPackage);
  writeFileSync(path.join(maliciousPackage, "__init__.py"), "");
  writeFileSync(
    path.join(maliciousPackage, "__main__.py"),
    "from pathlib import Path\nPath(__file__).resolve().parents[1].joinpath('hijacked.txt').write_text('hijacked')\n",
  );

  try {
    const result = childProcess.spawnSync(process.execPath, [launcherPath, "runs"], {
      cwd: maliciousRoot,
      env: { ...process.env },
      encoding: "utf8",
      timeout: 30000,
    });

    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "");
    assert.equal(existsSync(marker), false, "working-directory package executed");
    assert.equal(existsSync(path.join(maliciousRoot, ".agentpermit", "runs.sqlite")), true);
  } finally {
    rmSync(maliciousRoot, { recursive: true, force: true });
  }
});

test("POSIX launcher forwards every signal while active and none after cleanup", () => {
  const { runLauncher } = loadLauncher();
  const parent = new EventEmitter();
  const child = new EventEmitter();
  child.pid = 4100;
  const processKills = [];
  const directKills = [];
  child.kill = (value) => { directKills.push(value); };

  runLauncher({
    argv: [],
    env: { AGENTPERMIT_PYTHON: "python-ok" },
    platform: "linux",
    spawnSync: versionProbe({ "python-ok": "3.12.2" }, []),
    spawn: () => child,
    signalSource: parent,
    processKill(pid, value) { processKills.push([pid, value]); },
    onExit: () => {},
  });

  parent.emit("SIGINT");
  parent.emit("SIGINT");
  parent.emit("SIGTERM");
  parent.emit("SIGHUP");
  assert.deepEqual(processKills, [
    [-child.pid, "SIGINT"],
    [-child.pid, "SIGINT"],
    [-child.pid, "SIGTERM"],
    [-child.pid, "SIGHUP"],
  ]);
  assert.deepEqual(directKills, []);

  child.emit("exit", null, "SIGTERM");
  assert.deepEqual(
    ["SIGINT", "SIGTERM", "SIGHUP"].map((value) => parent.listenerCount(value)),
    [0, 0, 0],
  );
  parent.emit("SIGINT");
  parent.emit("SIGTERM");
  assert.equal(processKills.length, 4);
});

test("POSIX signal forwarding tolerates a child-exit race", () => {
  const { runLauncher } = loadLauncher();
  const parent = new EventEmitter();
  const child = new EventEmitter();
  child.pid = 4300;
  const directKills = [];
  child.kill = (signal) => { directKills.push(signal); };

  runLauncher({
    argv: [],
    env: { AGENTPERMIT_PYTHON: "python-ok" },
    platform: "linux",
    spawnSync: versionProbe({ "python-ok": "3.12.2" }, []),
    spawn: () => child,
    signalSource: parent,
    processKill() {
      throw Object.assign(new Error("process already exited"), { code: "ESRCH" });
    },
    onExit: () => {},
  });

  assert.doesNotThrow(() => parent.emit("SIGTERM"));
  assert.deepEqual(directKills, []);
  child.emit("exit", null, "SIGTERM");
  assert.equal(parent.listenerCount("SIGTERM"), 0);
});

test("Windows launcher shares the console process group without explicit forwarding handlers", () => {
  const { runLauncher } = loadLauncher();
  const parent = new EventEmitter();
  const child = new EventEmitter();
  const spawnCalls = [];
  const processKills = [];
  const exitCodes = [];

  runLauncher({
    argv: [],
    env: { AGENTPERMIT_PYTHON: "python-ok" },
    platform: "win32",
    spawnSync: versionProbe({ "python-ok": "3.12.2" }, []),
    spawn(command, args, options) {
      spawnCalls.push({ command, args, options });
      return child;
    },
    signalSource: parent,
    processKill(pid, signal) { processKills.push([pid, signal]); },
    setExitCode(code) { exitCodes.push(code); },
  });

  assert.equal(spawnCalls[0].options.detached, false);
  assert.deepEqual(
    ["SIGINT", "SIGTERM", "SIGHUP"].map((signal) => parent.listenerCount(signal)),
    [0, 0, 0],
  );
  parent.emit("SIGINT");
  parent.emit("SIGTERM");
  const originalExitCode = process.exitCode;
  child.emit("exit", null, "SIGINT");
  process.exitCode = originalExitCode;
  assert.deepEqual(processKills, []);
  assert.deepEqual(exitCodes, [1]);
});

test("parent completion re-signals only on POSIX", () => {
  const { completeParent } = loadLauncher();
  assert.equal(typeof completeParent, "function");
  const processKills = [];
  const exitCodes = [];
  const dependencies = {
    pid: 4400,
    processKill(pid, signal) { processKills.push([pid, signal]); },
    setExitCode(code) { exitCodes.push(code); },
  };

  completeParent(null, "SIGINT", { ...dependencies, platform: "win32" });
  assert.deepEqual(processKills, []);
  assert.deepEqual(exitCodes, [1]);

  completeParent(null, "SIGTERM", { ...dependencies, platform: "linux" });
  assert.deepEqual(processKills, [[4400, "SIGTERM"]]);
  assert.deepEqual(exitCodes, [1]);
});

test("asynchronous spawn errors complete once without uncaught errors or signal leaks", () => {
  const { runLauncher } = loadLauncher();
  const parent = new EventEmitter();
  const child = new EventEmitter();
  child.pid = 4200;
  const errors = [];
  const exits = [];

  runLauncher({
    argv: [],
    env: { AGENTPERMIT_PYTHON: "python-ok" },
    platform: "linux",
    spawnSync: versionProbe({ "python-ok": "3.12.2" }, []),
    spawn: () => child,
    signalSource: parent,
    stderr: { write(message) { errors.push(message); } },
    onExit(code, signal) { exits.push([code, signal]); },
  });

  assert.doesNotThrow(() => child.emit("error", new Error("spawn EACCES")));
  child.emit("exit", 0, null);
  assert.deepEqual(exits, [[1, null]]);
  assert.match(errors.join(""), /failed to start Python/i);
  assert.match(errors.join(""), /EACCES/);
  assert.equal(child.listenerCount("error"), 0);
  assert.equal(child.listenerCount("exit"), 0);
  assert.deepEqual(
    ["SIGINT", "SIGTERM", "SIGHUP"].map((signal) => parent.listenerCount(signal)),
    [0, 0, 0],
  );
});

test("missing suitable Python prints an actionable error and does not spawn", () => {
  const { runLauncher } = loadLauncher();
  const errors = [];
  let spawned = false;

  const result = runLauncher({
    argv: [],
    env: {},
    platform: "linux",
    spawnSync: versionProbe({}, []),
    spawn() {
      spawned = true;
    },
    stderr: { write(message) { errors.push(message); } },
  });

  assert.equal(result, null);
  assert.equal(spawned, false);
  assert.match(errors.join(""), /AgentPermit requires Python 3\.10 or newer/);
  assert.match(errors.join(""), /AGENTPERMIT_PYTHON/);
});
