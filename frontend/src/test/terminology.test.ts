import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const BANNED_TERM = "insur" + "ance";

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      out.push(...walk(p));
    } else if (p.endsWith(".ts") || p.endsWith(".tsx") || p.endsWith(".html")) {
      out.push(p);
    }
  }
  return out;
}

const srcDir = join(import.meta.dirname, "..");

describe("terminology compliance (REQ-NF-05)", () => {
  it("no source file mentions the prohibited word", () => {
    const files = walk(srcDir);
    expect(files.length).toBeGreaterThan(10);
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      expect(text.toLowerCase(), `${file} contains a banned term`).not.toContain(BANNED_TERM);
    }
  });
});
