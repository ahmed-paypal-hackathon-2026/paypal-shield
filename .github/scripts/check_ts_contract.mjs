#!/usr/bin/env node
// REQ-OPS-02: sanity-check frontend/src/types/contracts.ts against the frozen
// OpenAPI component schemas — every TS interface field must exist in the spec.

import { readFileSync, statSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const specPath = resolve(root, "shared", "openapi.json");
const tsPath = resolve(root, "frontend", "src", "types", "contracts.ts");

if (statSync(specPath).size === 0 || statSync(tsPath).size === 0) {
  console.log("contract freeze not landed yet (P4-owned files empty); skipping");
  process.exit(0);
}

const spec = JSON.parse(readFileSync(specPath, "utf8"));
const ts = readFileSync(tsPath, "utf8");

const schemas = spec.components?.schemas ?? {};
const mapping = {
  PillarScores: "PillarScores",
  RiskAssessment: "RiskAssessment",
  ProtectionQuote: "ProtectionQuote",
  ClaimRecord: "ClaimRecord",
  LedgerSummary: "LedgerSummary",
  TransactionRow: "TransactionRow",
  MerchantTrustRow: "MerchantTrustRow",
};

const interfaceBlocks = ts.split(/export interface (\w+ \{)/).slice(1);
const parsed = new Map();
for (let i = 0; i < interfaceBlocks.length; i += 2) {
  const name = interfaceBlocks[i].split(" ")[0];
  const body = interfaceBlocks[i + 1].split("\n}");
  parsed.set(name, body[0]);
}

const errors = [];
for (const [tsName, schemaName] of Object.entries(mapping)) {
  const schema = schemas[schemaName];
  if (!schema) {
    errors.push(`schema ${schemaName} missing from openapi.json`);
    continue;
  }
  const required = new Set(schema.required ?? []);
  const block = parsed.get(tsName);
  if (!block) {
    errors.push(`interface ${tsName} missing from contracts.ts`);
    continue;
  }
  for (const field of required) {
    const pattern = new RegExp(`\\b${field}\\??\\s*:`);
    if (!pattern.test(block)) {
      errors.push(`contracts.ts ${tsName} is missing required field ${field}`);
    }
  }
}

if (errors.length > 0) {
  for (const e of errors) console.error(`CONTRACT SYNC ERROR: ${e}`);
  process.exit(1);
}
console.log("TypeScript contract is in sync with shared/openapi.json");
