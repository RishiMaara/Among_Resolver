import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Paise as rupees: 6852202 → "₹68,522.02". Indian digit grouping (lakh,
 * crore), because a controller reading against crore thresholds should not
 * have to count digits; Intl's en-IN locale does this where a plain
 * toLocaleString does not.
 */
export function inr(paise: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
}
