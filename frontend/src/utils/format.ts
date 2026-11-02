const symbols: Record<string, string> = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  HUF: "Ft ",
  TWD: "$",
};

/** Formats a decimal Money string for display. No numeric conversion. */
export function formatMoney(money: string, currency: string): string {
  const symbol = symbols[currency];
  return symbol ? `${symbol}${money}` : `${currency} ${money}`;
}

export function formatRatio(ratio: string): string {
  return `${ratio}% net loss`;
}

export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
