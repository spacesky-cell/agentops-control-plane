#!/usr/bin/env node

"use strict";

const childProcess = require("node:child_process");
const path = require("node:path");

const MINIMUM_PYTHON = [3, 10];
const VERSION_SCRIPT = "import sys; print('.'.join(map(str, sys.version_info[:3])))";
const BOOTSTRAP_SCRIPT = [
  "import runpy, sys",
  "package_root = sys.argv[1]",
  "user_args = sys.argv[2:]",
  "sys.path.insert(0, package_root)",
  "sys.argv = ['agentpermit', *user_args]",
  "runpy.run_module('agentpermit', run_name='__main__', alter_sys=True)",
].join("; ");

function candidates(env, platform) {
  const result = [];
  if (env.AGENTPERMIT_PYTHON) {
    result.push({ command: env.AGENTPERMIT_PYTHON, prefixArgs: [] });
  }
  if (platform === "win32") {
    result.push({ command: "py", prefixArgs: ["-3"] });
  }
  result.push({ command: "python3", prefixArgs: [] });
  result.push({ command: "python", prefixArgs: [] });
  return result;
}

function isSupportedVersion(output) {
  const match = String(output).trim().match(/^(\d+)\.(\d+)(?:\.\d+)?$/);
  if (!match) {
    return false;
  }
  const version = [Number(match[1]), Number(match[2])];
  return version[0] > MINIMUM_PYTHON[0]
    || (version[0] === MINIMUM_PYTHON[0] && version[1] >= MINIMUM_PYTHON[1]);
}

function findPython({
  env = process.env,
  platform = process.platform,
  spawnSync = childProcess.spawnSync,
} = {}) {
  for (const candidate of candidates(env, platform)) {
    const probe = spawnSync(
      candidate.command,
      [...candidate.prefixArgs, "-I", "-c", VERSION_SCRIPT],
      { encoding: "utf8", windowsHide: true },
    );
    if (!probe.error && probe.status === 0 && isSupportedVersion(probe.stdout)) {
      return candidate;
    }
  }
  return null;
}

function installSignalForwarding(child, signalSource, platform, processKill) {
  if (platform === "win32") {
    return () => {};
  }

  const signals = ["SIGINT", "SIGTERM", "SIGHUP"];
  let active = true;
  const handlers = signals.map((signal) => {
    const handler = () => {
      if (!active) {
        return;
      }
      try {
        processKill(-child.pid, signal);
      } catch (error) {
        if (error && error.code !== "ESRCH") {
          try {
            child.kill(signal);
          } catch {
            // The child exited between signal receipt and delivery.
          }
        }
      }
    };
    signalSource.on(signal, handler);
    return [signal, handler];
  });

  return () => {
    if (!active) {
      return;
    }
    active = false;
    for (const [signal, handler] of handlers) {
      signalSource.removeListener(signal, handler);
    }
  };
}

function completeParent(code, signal, {
  platform = process.platform,
  pid = process.pid,
  processKill = process.kill,
  setExitCode = (value) => { process.exitCode = value; },
} = {}) {
  if (signal && platform !== "win32") {
    processKill(pid, signal);
    return;
  }
  setExitCode(code === null ? 1 : code);
}

function runLauncher({
  argv = process.argv.slice(2),
  env = process.env,
  platform = process.platform,
  packageRoot = path.resolve(__dirname, ".."),
  spawnSync = childProcess.spawnSync,
  spawn = childProcess.spawn,
  signalSource = process,
  processKill = process.kill,
  setExitCode = (value) => { process.exitCode = value; },
  stderr = process.stderr,
  onExit = (code, signal) => completeParent(
    code,
    signal,
    { platform, processKill, setExitCode },
  ),
} = {}) {
  const python = findPython({ env, platform, spawnSync });
  if (!python) {
    stderr.write(
      "AgentPermit requires Python 3.10 or newer. Install Python or set AGENTPERMIT_PYTHON to a suitable interpreter.\n",
    );
    return null;
  }

  const child = spawn(
    python.command,
    [...python.prefixArgs, "-I", "-c", BOOTSTRAP_SCRIPT, path.resolve(packageRoot), ...argv],
    { env, stdio: "inherit", detached: platform !== "win32" },
  );
  const cleanupSignals = installSignalForwarding(child, signalSource, platform, processKill);
  let completed = false;
  const complete = (code, signal, error = null) => {
    if (completed) {
      return;
    }
    completed = true;
    cleanupSignals();
    child.removeListener("error", handleError);
    child.removeListener("exit", handleExit);
    if (error) {
      stderr.write(
        `AgentPermit failed to start Python: ${error.message}. Check AGENTPERMIT_PYTHON and interpreter permissions.\n`,
      );
    }
    onExit(code, signal);
  };
  const handleError = (error) => complete(1, null, error);
  const handleExit = (code, signal) => complete(code, signal);
  child.once("error", handleError);
  child.once("exit", handleExit);
  return child;
}

if (require.main === module) {
  const child = runLauncher();
  if (!child) {
    process.exitCode = 1;
  }
}

module.exports = { completeParent, findPython, isSupportedVersion, runLauncher };
