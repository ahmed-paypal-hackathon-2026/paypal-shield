const descriptions: Record<string, string> = {
  VIS_AI_GENERATED: "Listing photo shows synthetic-conversion artifacts",
  VIS_TIMEOUT: "Visual check timed out; conservative default applied",
  SEM_LOW_ALIGNMENT: "Photo does not match the product text",
  REV_INSUFFICIENT: "Fewer than five reviews; neutral score used",
  REV_KEYWORD_KNOCKOFF: "Reviews mention knockoff or counterfeit phrases",
  REV_KEYWORD_CHEAP_PLASTIC: "Reviews mention cheap-plastic phrasing",
  REV_KEYWORD_NOT_AS_PICTURED: "Reviews mention not-as-pictured phrasing",
  REV_BOT_PATTERN: "Reviews look bot-generated",
  PRICE_EXTREME_UNDER: "Price is extreme underpricing for this category",
  PRICE_INSUFFICIENT_DATA: "Not enough category prices; neutral score used",
  REP_FLAGGED_HISTORY: "Merchant has verified fraud claims on record",
  CACHE_MISS_DEFAULT: "Risk pillars still computing; conservative defaults used",
  ARB_DISCREPANCY_HIGH: "Received item differs strongly from the listing photo",
  ARB_DISCREPANCY_LOW: "Received item matches the listing photo",
  ARB_AMOUNT_OVER_CAP: "Claim exceeds the automated payout cap",
  ARB_AMOUNT_OK: "Claim amount within the automated payout cap",
  ARB_VELOCITY_LIMIT: "Buyer claim velocity over the limit",
  ARB_VELOCITY_OK: "Buyer claim velocity within limits",
  ARB_DISPUTE_ACTIVE: "An external dispute is open on this order",
  ARB_DISPUTE_NONE: "No external dispute on this order",
};

export function describeReason(code: string): string {
  return descriptions[code] ?? code;
}
