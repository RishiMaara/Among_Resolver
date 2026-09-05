/**
 * Saved fee terms, per merchant.
 *
 * The engine accepts gateway_fee_bps / tax_withholding_bps / flat_fee_cents
 * per run, but the UI never sent them — so every reconciliation used
 * DEFAULT_RATE_CARD, which the engine's own docstring calls "a plausible
 * guess and nobody's actual contract".
 *
 * That guess is not harmless. Subset-sum is exact, so a card that is wrong by
 * a fraction of a percent does not degrade the match, it eliminates it: the
 * engine measures a 1.5 basis point error taking auto-clear to 0%. A team
 * reconciling three merchants on three contracts had no way to say so, and no
 * way to tell that this was why nothing cleared.
 *
 * Profiles live in localStorage, which is the honest scope of this: it is
 * per-browser, not per-account, and it does not version terms by effective
 * date. Contract terms change, and a settlement from March must be
 * reconciled on March's card — that needs a backend and is not built. What
 * this removes is the retyping.
 */

const KEY = "among.ratecards";

export interface RateCard {
  name: string;
  gatewayFeeBps: string;
  taxWithholdingBps: string;
  flatFeeCents: string;
}

export function loadRateCards(): RateCard[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as RateCard[]) : [];
  } catch {
    return []; // storage disabled, or corrupt JSON
  }
}

export function saveRateCard(card: RateCard): RateCard[] {
  const cards = loadRateCards().filter(
    (c) => c.name.trim().toLowerCase() !== card.name.trim().toLowerCase(),
  );
  const next = [...cards, card].sort((a, b) => a.name.localeCompare(b.name));
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* nothing persisted; the values still apply to this run */
  }
  return next;
}

export function deleteRateCard(name: string): RateCard[] {
  const next = loadRateCards().filter((c) => c.name !== name);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
  return next;
}
