import type { Money } from "./contracts";

export interface QuoteRequest {
  merchant_id: string;
  item_hash: string;
  item_title: string;
  item_price: Money;
  currency_code: string;
}

export interface CreateOrderRequest {
  quote_id: string;
  opt_in: boolean;
  buyer_ref: string;
}

export interface CreateOrderResponse {
  order_id: string;
  internal_order_id: string;
  status: string;
  total: Money;
  currency_code: string;
}

export interface CaptureResponse {
  order_id: string;
  paypal_capture_id: string;
  status: string;
}

export interface HealthStatus {
  status: string;
  paypal_sandbox_connected: boolean;
  demo_mode: boolean;
}
