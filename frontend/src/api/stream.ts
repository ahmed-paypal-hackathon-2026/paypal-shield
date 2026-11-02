import type { ClaimRecord, MerchantTrustRow, TransactionRow } from "../types/contracts";

export type Topic = "transaction" | "claim" | "merchant";

export type StreamEnvelope =
  | { topic: "transaction"; action: "add" | "update"; record: TransactionRow }
  | { topic: "claim"; action: "add" | "update"; record: ClaimRecord }
  | { topic: "merchant"; action: "update"; record: MerchantTrustRow };

export type StreamHandler = (envelope: StreamEnvelope) => void;

export type Unsubscribe = () => void;
