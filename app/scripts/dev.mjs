import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function supervise(children) {
  let stopping = false;
  let exitCode = 0;
  let remaining = children.length;
  let resolveDone;
  const done = new Promise((resolve) => { resolveDone = resolve; });
  const onInterrupt = () => stop(130);
  const onTerminate = () => stop(143);
  process.on("SIGINT", onInterrupt);
  process.on("SIGTERM", onTerminate);

  function stop(code) {
    if (stopping) return;
    stopping = true;
    exitCode = code;
    for (const child of children) {
      if (!child.pid || child.exitCode !== null || child.signalCode !== null) continue;
      if (process.platform === "win32") {
        // Node's watch server has its own child; terminate the entire owned tree.
        spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
      } else {
        child.kill("SIGTERM");
      }
    }
  }

  for (const child of children) {
    child.on("error", (error) => {
      console.error(`Could not start development process: ${error.message}`);
      stop(1);
    });
    child.on("close", (code) => {
      if (!stopping) stop(code ?? 1);
      if (--remaining === 0) {
        process.removeListener("SIGINT", onInterrupt);
        process.removeListener("SIGTERM", onTerminate);
        resolveDone(exitCode);
      }
    });
  }
  return { done, stop };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const compiler = spawn(process.execPath, [
    "node_modules/typescript/bin/tsc", "-p", "tsconfig.json",
    "--watch", "--preserveWatchOutput",
  ], { cwd: appRoot, stdio: "inherit" });
  const server = spawn(process.execPath, [
    "--watch", ...(process.platform === "linux" ? [] : ["--watch-path=dist/web/src/server"]),
    "dist/web/src/server/main.js", ...process.argv.slice(2),
  ], { cwd: appRoot, stdio: "inherit", env: { ...process.env, WEBUI_DEV_RELOAD: "1" } });
  process.exitCode = await supervise([compiler, server]).done;
}
