import { stopStream } from "./actions";
import { useGh } from "./github";
import { useApp, type SecretName } from "./store";
import {
  errText,
  isTauri,
  vaultClearAll,
  vaultDeleteSecret,
  vaultLoadSecrets,
  vaultSaveSecret,
  type VaultKey,
} from "./tauri";
import { toastErr, toastOk } from "./toast";

export const SECRET_TO_VAULT: Partial<Record<SecretName, VaultKey>> = {
  api: "backend_api_token",
  github: "github_token",
  openrouter: "openrouter_api_key",
  commandcode: "commandcode_api_key",
  opencodeGo: "opencode_go_api_key",
};

export const VAULT_TO_SECRET: Record<VaultKey, SecretName> = {
  backend_api_token: "api",
  github_token: "github",
  openrouter_api_key: "openrouter",
  commandcode_api_key: "commandcode",
  opencode_go_api_key: "opencodeGo",
};

export const VAULT_KEY_LABELS: Record<VaultKey, string> = {
  backend_api_token: "Backend API token",
  github_token: "GitHub token",
  openrouter_api_key: "OpenRouter key",
  commandcode_api_key: "Command Code key",
  opencode_go_api_key: "OpenCode Go key",
};

let hydrating = false;
const pendingTimers = new Map<VaultKey, ReturnType<typeof setTimeout>>();

function cancelPendingVaultWrites() {
  for (const timer of pendingTimers.values()) clearTimeout(timer);
  pendingTimers.clear();
}

function addVaultKey(key: VaultKey) {
  const cur = useApp.getState().vaultKeys;
  if (!cur.includes(key)) useApp.getState().setVaultKeys([...cur, key]);
}

function removeVaultKey(key: VaultKey) {
  const cur = useApp.getState().vaultKeys;
  if (cur.includes(key)) useApp.getState().setVaultKeys(cur.filter((k) => k !== key));
}

function scheduleVaultSync(secretName: SecretName, vaultKey: VaultKey) {
  const existing = pendingTimers.get(vaultKey);
  if (existing) clearTimeout(existing);
  const timer = setTimeout(() => {
    pendingTimers.delete(vaultKey);
    const state = useApp.getState();
    if (!state.rememberSecrets || !isTauri()) return;
    const latest = (state.secrets[secretName] || "").trim();
    if (latest) {
      void vaultSaveSecret(vaultKey, latest)
        .then((ok) => {
          if (ok) addVaultKey(vaultKey);
        })
        .catch(() => {
          /* surfaced on explicit save; avoid noisy toasts per keystroke */
        });
    } else {
      void vaultDeleteSecret(vaultKey)
        .then((ok) => {
          if (ok) removeVaultKey(vaultKey);
        })
        .catch(() => {});
    }
  }, 250);
  pendingTimers.set(vaultKey, timer);
}

// Auto-sync secret edits to the OS-protected vault when the user opted in.
// Debounced per key so rapid typing never races out-of-order writes, and skipped
// during boot hydration or session-only wipes (where clearNonce advances).
useApp.subscribe((state, prev) => {
  if (state.clearNonce !== prev.clearNonce) {
    cancelPendingVaultWrites();
    return;
  }
  if (hydrating || !state.rememberSecrets || !isTauri()) {
    return;
  }
  for (const [secretName, vaultKey] of Object.entries(SECRET_TO_VAULT) as [SecretName, VaultKey][]) {
    if (state.secrets[secretName] !== prev.secrets[secretName]) {
      scheduleVaultSync(secretName, vaultKey);
    }
  }
});

/** Load OS-protected vault secrets into the in-memory session on app boot. */
export async function hydrateFromVault(): Promise<VaultKey[]> {
  hydrating = true;
  try {
    const loaded = await vaultLoadSecrets();
    const keys = Object.keys(loaded) as VaultKey[];
    const state = useApp.getState();
    for (const key of keys) {
      const val = loaded[key];
      const secretName = VAULT_TO_SECRET[key];
      if (val && secretName && !state.secrets[secretName]) {
        state.setSecret(secretName, val);
      }
    }
    state.setVaultKeys(keys);
    let storedPref: string | null = null;
    try {
      storedPref = localStorage.getItem("pragent.remember_secrets");
    } catch {
      storedPref = null;
    }
    if (keys.length > 0 && storedPref === null) {
      state.setRememberSecrets(true);
    }
    state.setVaultLoaded(true);
    return keys;
  } catch (e) {
    useApp.getState().setVaultLoaded(true);
    toastErr(
      "Could not read local PC vault",
      `${errText(e, 180)} — Open Settings to wipe and recreate the local vault if corrupted.`,
    );
    return [];
  } finally {
    hydrating = false;
  }
}

/** Save all currently populated supported session secrets to the OS-protected vault. */
export async function syncAllSecretsToVault(): Promise<number> {
  if (!isTauri()) return 0;
  const secrets = useApp.getState().secrets;
  let saved = 0;
  const stored: VaultKey[] = [...useApp.getState().vaultKeys];
  for (const [secretName, vaultKey] of Object.entries(SECRET_TO_VAULT) as [SecretName, VaultKey][]) {
    const val = (secrets[secretName] || "").trim();
    if (!val) continue;
    const ok = await vaultSaveSecret(vaultKey, val);
    if (ok) {
      saved++;
      if (!stored.includes(vaultKey)) stored.push(vaultKey);
    }
  }
  useApp.getState().setVaultKeys(stored);
  return saved;
}

/** Toggle opt-in Windows DPAPI persistence for secrets on this PC. */
export async function setRememberOnPc(enabled: boolean): Promise<boolean> {
  useApp.getState().setRememberSecrets(enabled);
  if (!enabled) {
    toastOk("Auto-save paused", "New secret edits stay in session only. Use Wipe local vault to remove already-saved keys.");
    return true;
  }
  if (!isTauri()) {
    toastOk("Preference saved", "Windows DPAPI vault activates automatically in the desktop app.");
    return false;
  }
  try {
    const count = await syncAllSecretsToVault();
    toastOk(
      "Secure local persistence enabled",
      count > 0
        ? `Encrypted and saved ${count} secret(s) with Windows DPAPI.`
        : "Future tokens you enter will be encrypted with Windows DPAPI on this PC.",
    );
    return true;
  } catch (e) {
    toastErr("Vault save failed", errText(e, 220));
    return false;
  }
}

/** Save one specific secret directly to the local OS vault (used by Setup Wizard / Settings). */
export async function saveSecretToVault(name: SecretName, value: string): Promise<boolean> {
  const vaultKey = SECRET_TO_VAULT[name];
  if (!vaultKey || !isTauri()) return false;
  const trimmed = value.trim();
  if (!trimmed) {
    const ok = await vaultDeleteSecret(vaultKey);
    if (ok) removeVaultKey(vaultKey);
    return ok;
  }
  const ok = await vaultSaveSecret(vaultKey, trimmed);
  if (ok) addVaultKey(vaultKey);
  return ok;
}

/** Delete all secrets stored in the OS-protected local vault without touching active session values. */
export async function wipeLocalVault(silent = false): Promise<boolean> {
  cancelPendingVaultWrites();
  try {
    const ok = await vaultClearAll();
    useApp.getState().setVaultKeys([]);
    useApp.getState().setRememberSecrets(false);
    if (!silent) {
      toastOk("Local vault wiped", ok ? "Removed all encrypted secrets from this PC." : "Local vault preference cleared.");
    }
    return ok;
  } catch (e) {
    if (!silent) toastErr("Vault wipe failed", errText(e, 220));
    return false;
  }
}

/** Wipe every session secret, stop the live stream and reset the GitHub session. */
export function forgetSecrets() {
  stopStream();
  useGh.getState().reset();
  useApp.getState().wipeSecrets();
  const hasVault = useApp.getState().vaultKeys.length > 0;
  toastOk(
    "Session secrets cleared",
    hasVault
      ? "Session values wiped. Local PC vault still holds saved keys — wipe it in Settings if needed."
      : "Session values and stream tokens wiped.",
  );
}

/** Wipe both active session secrets AND the OS-protected local vault. */
export async function forgetSecretsAndVault(): Promise<void> {
  stopStream();
  useGh.getState().reset();
  useApp.getState().wipeSecrets();
  await wipeLocalVault(true);
  toastOk("All secrets wiped", "Session memory, stream tokens, and local DPAPI vault cleared.");
}
