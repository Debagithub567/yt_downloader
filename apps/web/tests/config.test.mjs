import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../next.config.ts";

test("API rewrite accepts the full configured media upload size", async () => {
  const rewrites = await nextConfig.rewrites();
  assert.ok(rewrites.some(({ source }) => source === "/api/:path*"));

  // media_core's default max_input_bytes is 512 MiB.
  const apiInputLimit = 512 * 1024 * 1024;
  assert.ok(nextConfig.experimental.proxyClientMaxBodySize > apiInputLimit);
});
