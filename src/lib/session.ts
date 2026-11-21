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
  vaultStatus,
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

/** True once initial boot hydration of the local vault has completed (success, error, or unsupported). */
export const isVaultHydrated = (): boolean => useApp.getState().vaultLoaded;

/** Reactive hook exposing whether initial boot vault hydration has completed. */
export const useVaultHydrated = (): boolean => useApp((s) => s.vaultLoaded);

let suppressAutosave = false;
let wipeEpoch = 0;
let vaultQueue: Promise<unknown> = Promise.resolve();
let activeHydrationPromise: Promise<VaultKey[]> | null = null;
const pendingTimers = new Map<VaultKey, ReturnType<typeof setTimeout>>();

function enqueueVaultOp<T>(op: () => Promise<T>): Promise<T> {
  const run = vaultQueue.then(op, op);
  vaultQueue = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

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
  const scheduledEpoch = wipeEpoch;
  const timer = setTimeout(() => {
    pendingTimers.delete(vaultKey);
    if (scheduledEpoch !== wipeEpoch) return;
    void enqueueVaultOp(async () => {
      // Wait for boot capability check if an edit happened before vaultStatus() resolved.
      if (!useApp.getState().vaultLoaded && activeHydrationPromise) {
        await activeHydrationPromise.catch(() => {});
      }
      if (scheduledEpoch !== wipeEpoch) return;
      const state = useApp.getState();
      if (!state.rememberSecrets || !state.vaultSupported || !isTauri()) return;
      const latest = (state.secrets[secretName] || "").trim();
      if (latest) {
        const ok = await vaultSaveSecret(vaultKey, latest);
        if (ok && scheduledEpoch === wipeEpoch) {
          addVaultKey(vaultKey);
          useApp.getState().setVaultError("");
        }
      } else {
        const ok = await vaultDeleteSecret(vaultKey);
        if (ok && scheduledEpoch === wipeEpoch) {
          removeVaultKey(vaultKey);
        }
      }
    }).catch(() => {
      /* surfaced on explicit save; avoid noisy toasts per keystroke */
    });
  }, 250);
  pendingTimers.set(vaultKey, timer);
}

// Auto-sync secret edits to the OS-protected vault when the user opted in.
// Debounced per key and serialized through `vaultQueue` so rapid edits always
// persist the latest value in order. Only synchronous hydration writes and
// session-only wipes (clearNonce) are excluded.
useApp.subscribe((state, prev) => {
  if (state.clearNonce !== prev.clearNonce) {
    cancelPendingVaultWrites();
    return;
  }
  if (suppressAutosave || !state.rememberSecrets || !isTauri()) {
    return;
  }
  if (state.vaultLoaded && !state.vaultSupported) {
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
  const run = (async (): Promise<VaultKey[]> => {
    const startClearNonce = useApp.getState().clearNonce;
    const startWipeEpoch = wipeEpoch;
    try {
      const status = await vaultStatus();
      useApp.getState().setVaultSupported(status.supported);
      if (!status.supported) {
        if (useApp.getState().rememberSecrets) {
          useApp.getState().setRememberSecrets(false);
        }
        useApp.getState().setVaultKeys([]);
        useApp.getState().setVaultError("");
        useApp.getState().setVaultLoaded(true);
        return [];
      }

      const loaded = await vaultLoadSecrets();
      const keys = Object.keys(loaded) as VaultKey[];
      const cur = useApp.getState();

      // Discard hydration results if session secrets or the local vault were wiped while awaiting.
      if (cur.clearNonce !== startClearNonce || wipeEpoch !== startWipeEpoch) {
        cur.setVaultLoaded(true);
        return [];
      }

      // Suppress autosave ONLY for the synchronous store updates made by hydration itself.
      suppressAutosave = true;
      try {
        for (const key of keys) {
          const val = loaded[key];
          const secretName = VAULT_TO_SECRET[key];
          if (val && secretName && !useApp.getState().secrets[secretName]) {
            useApp.getState().setSecret(secretName, val);
          }
        }
        useApp.getState().setVaultKeys(keys);
        useApp.getState().setVaultError("");
        let storedPref: string | null = null;
        try {
          storedPref = localStorage.getItem("pragent.remember_secrets");
        } catch {
          storedPref = null;
        }
        if (keys.length > 0 && storedPref === null) {
          useApp.getState().setRememberSecrets(true);
        }
      } finally {
        suppressAutosave = false;
      }

      useApp.getState().setVaultLoaded(true);
      return keys;
    } catch (e) {
      const msg = errText(e, 180);
      useApp.getState().setVaultError(msg);
      useApp.getState().setVaultLoaded(true);
      toastErr(
        "Could not read local PC vault",
        `${msg} — Open Settings to wipe and recreate the local vault if corrupted.`,
      );
      return [];
    }
  })();
  activeHydrationPromise = run;
  try {
    return await run;
  } finally {
    if (activeHydrationPromise === run) activeHydrationPromise = null;
  }
}

/** Save all currently populated supported session secrets to the OS-protected vault. */
export async function syncAllSecretsToVault(): Promise<number> {
  if (!isTauri() || !useApp.getState().vaultSupported) return 0;
  const syncEpoch = wipeEpoch;
  let saved = 0;
  for (const [secretName, vaultKey] of Object.entries(SECRET_TO_VAULT) as [SecretName, VaultKey][]) {
    if (syncEpoch !== wipeEpoch) break;
    const ok = await enqueueVaultOp(async () => {
      if (syncEpoch !== wipeEpoch) return false;
      const state = useApp.getState();
      if (!state.vaultSupported || !isTauri()) return false;
      const val = (state.secrets[secretName] || "").trim();
      if (!val) return false;
      const written = await vaultSaveSecret(vaultKey, val);
      if (written && syncEpoch === wipeEpoch) {
        addVaultKey(vaultKey);
        useApp.getState().setVaultError("");
      }
      return written;
    });
    if (ok) saved++;
  }
  return saved;
}

/** Toggle opt-in Windows DPAPI persistence for secrets on this PC. */
export async function setRememberOnPc(enabled: boolean): Promise<boolean> {
  if (!enabled) {
    useApp.getState().setRememberSecrets(false);
    toastOk("Auto-save paused", "New secret edits stay in session only. Use Wipe local vault to remove already-saved keys.");
    return true;
  }
  if (!isTauri() || !useApp.getState().vaultSupported) {
    useApp.getState().setRememberSecrets(false);
    toastErr(
      "Encrypted local vault unavailable",
      isTauri()
        ? "Encrypted local vault requires Windows DPAPI and is not supported on this OS. Tokens stay in session memory only."
        : "Windows DPAPI vault is only available in the Windows desktop app. Browser preview keeps tokens in sessionStorage only.",
    );
    return false;
  }
  useApp.getState().setRememberSecrets(true);
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
  if (!vaultKey || !isTauri() || !useApp.getState().vaultSupported) return false;
  const opEpoch = wipeEpoch;
  return enqueueVaultOp(async () => {
    if (opEpoch !== wipeEpoch) return false;
    if (!useApp.getState().vaultSupported || !isTauri()) return false;
    const trimmed = value.trim();
    if (!trimmed) {
      const ok = await vaultDeleteSecret(vaultKey);
      if (ok && opEpoch === wipeEpoch) removeVaultKey(vaultKey);
      return ok;
    }
    const ok = await vaultSaveSecret(vaultKey, trimmed);
    if (ok && opEpoch === wipeEpoch) {
      addVaultKey(vaultKey);
      useApp.getState().setVaultError("");
    }
    return ok;
  });
}

/** Delete all secrets stored in the OS-protected local vault without touching active session values. */
export async function wipeLocalVault(silent = false): Promise<boolean> {
  wipeEpoch += 1;
  cancelPendingVaultWrites();
  useApp.getState().setRememberSecrets(false);
  try {
    const ok = await enqueueVaultOp(async () => {
      const cleared = await vaultClearAll();
      if (cleared) {
        useApp.getState().setVaultKeys([]);
        useApp.getState().setVaultError("");
      }
      return cleared;
    });
    if (!silent) {
      if (ok) {
        toastOk("Local vault wiped", "Removed all encrypted secrets from this PC.");
      } else {
        toastErr("Local vault not wiped", "Local encrypted vault is unavailable on this platform.");
      }
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
  if (!useApp.getState().vaultSupported && !useApp.getState().vaultError) {
    useApp.getState().setRememberSecrets(false);
    toastOk("Session secrets cleared", "Session memory and stream tokens wiped (no local PC vault on this platform).");
    return;
  }
  const wiped = await wipeLocalVault(true);
  if (wiped) {
    toastOk("All secrets wiped", "Session memory, stream tokens, and local DPAPI vault cleared.");
  } else {
    toastErr(
      "Session cleared — local vault wipe failed",
      "Session memory and stream tokens were cleared, but the local PC vault could not be wiped. Retry in Settings.",
    );
  }
}
