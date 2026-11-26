// Engine entry point. Started directly in dev, or launched by the Tauri shell as a
// sidecar. Prints a single line "LC4_URL <url>" so the shell can read where to point.

import { startEngine } from "./server";

const engine = startEngine();
// The desktop shell parses this line to open the window at the engine URL.
console.log(`LC4_URL ${engine.url}`);

const shutdown = () => { engine.stop(); process.exit(0); };
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
