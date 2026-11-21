import * as React from "react";
import { Eraser, Plug, ShieldCheck, Trash2, Wifi, X } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section } from "@/components/common";
import { GithubConnect, GithubStateBadge } from "@/components/github-connect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { checkBackend } from "@/lib/actions";
import { cancelOAuth, connectOpenRouterOAuth, useOAuth } from "@/lib/oauth";
import {
  forgetSecrets,
  forgetSecretsAndVault,
  setRememberOnPc,
  syncAllSecretsToVault,
  VAULT_KEY_LABELS,
  wipeLocalVault,
} from "@/lib/session";
import { useApp } from "@/lib/store";
import { errText, isTauri } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

export function SettingsPage({ active }: { active: boolean }) {
  const backendUrl = useApp((s) => s.backendUrl);
  const setBackendUrl = useApp((s) => s.setBackendUrl);
  const backend = useApp((s) => s.backend);
  const apiToken = useApp((s) => s.secrets.api);
  const openrouter = useApp((s) => s.secrets.openrouter);
  const commandcode = useApp((s) => s.secrets.commandcode);
  const opencodeGo = useApp((s) => s.secrets.opencodeGo);
  const setSecret = useApp((s) => s.setSecret);
  const rememberSecrets = useApp((s) => s.rememberSecrets);
  const vaultSupported = useApp((s) => s.vaultSupported);
  const vaultError = useApp((s) => s.vaultError);
  const vaultKeys = useApp((s) => s.vaultKeys);
  const activeFlow = useOAuth((s) => s.activeFlow);

  const [urlDraft, setUrlDraft] = React.useState(backendUrl);
  const [out, setOut] = React.useState("Not tested.");
  const [confirmWipeAll, setConfirmWipeAll] = React.useState(false);
  React.useEffect(() => setUrlDraft(backendUrl), [backendUrl]);

  const commitUrl = () => {
    if (urlDraft.trim().replace(/\/+$/, "") === backendUrl) return;
    setBackendUrl(urlDraft);
    void checkBackend(false);
  };

  const test = async () => {
    setOut("Testing…");
    setBackendUrl(urlDraft);
    const url = useApp.getState().backendUrl; // the URL actually being tested
    const ok = await checkBackend(true);
    if (useApp.getState().backendUrl !== url) return; // selection changed while testing
    setOut(ok ? `OK — ${url}` : `Unreachable — is the backend running at ${url}?`);
  };

  return (
    <Page active={active}>
      <Section
        title="Backend connection"
        action={
          <Badge variant={backend.ok === true ? "success" : backend.ok === false ? "danger" : "outline"}>
            {backend.label}
          </Badge>
        }
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={test}>
              <Wifi /> Test connection
            </AsyncButton>
          </ButtonRow>
        }
      >
        <FieldGrid>
          <Field label="Backend URL" htmlFor="backend-url" hint="Stored in localStorage — not secret.">
            <Input
              id="backend-url"
              value={urlDraft}
              onChange={(e) => setUrlDraft(e.target.value)}
              onBlur={commitUrl}
              onKeyDown={(e) => e.key === "Enter" && commitUrl()}
              autoComplete="off"
            />
          </Field>
          <Field
            label="Backend API token"
            htmlFor="api-token"
            hint={
              vaultKeys.includes("backend_api_token")
                ? "Encrypted in local PC vault (Windows DPAPI) — restored automatically on launch."
                : "Session only unless Remember securely on this PC is enabled below."
            }
          >
            <SecretInput
              id="api-token"
              value={apiToken}
              placeholder="optional Bearer token"
              onChange={(e) => setSecret("api", e.target.value)}
            />
          </Field>
        </FieldGrid>

        {/* Opt-in Windows DPAPI local persistence */}
        <div className="bg-muted/25 grid gap-3 rounded-lg border p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-start gap-2.5">
              <ShieldCheck className="text-primary mt-0.5 size-4 shrink-0" />
              <div className="grid gap-0.5">
                <Label htmlFor="settings-remember-pc" className="cursor-pointer text-sm font-medium">
                  Remember securely on this PC (Windows DPAPI)
                </Label>
                <p className="text-muted-foreground text-xs leading-relaxed">
                  {vaultSupported
                    ? "Encrypts your Backend API token and provider keys with your Windows user profile so you don't have to re-paste them after restarting the app. Never touches localStorage."
                    : isTauri()
                      ? "Encrypted local vault requires Windows DPAPI and is not supported on this OS. Tokens stay in memory + sessionStorage only."
                      : "Available in the Windows desktop app (Windows DPAPI). Browser preview keeps tokens in sessionStorage only."}
                </p>
              </div>
            </div>
            <Switch
              id="settings-remember-pc"
              checked={vaultSupported && rememberSecrets}
              disabled={!vaultSupported}
              onCheckedChange={(v) => void setRememberOnPc(v)}
            />
          </div>

          {vaultError ? (
            <div className="border-destructive/40 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-xs">
              Local vault could not be loaded: {vaultError}. Click <b>Wipe local vault</b> below to clear and recreate it.
            </div>
          ) : null}

          {vaultSupported || vaultError ? (
            <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-muted-foreground text-xs">Stored in local vault:</span>
                {vaultKeys.length > 0 ? (
                  vaultKeys.map((k) => (
                    <Badge key={k} variant="secondary" className="text-[10px]">
                      {VAULT_KEY_LABELS[k] || k}
                    </Badge>
                  ))
                ) : (
                  <Badge variant={vaultError ? "danger" : "outline"} className="text-[10px]">
                    {vaultError ? "unreadable" : "none"}
                  </Badge>
                )}
              </div>
              <ButtonRow>
                {vaultSupported && rememberSecrets ? (
                  <AsyncButton
                    variant="outline"
                    size="xs"
                    onClick={async () => {
                      try {
                        const res = await syncAllSecretsToVault();
                        if (!res.canceled) {
                          toastOk("Vault synced", `Saved ${res.saved} active secret(s) to Windows DPAPI vault.`);
                        }
                      } catch (e) {
                        toastErr("Vault sync failed", errText(e, 220));
                      }
                    }}
                  >
                    Sync current secrets
                  </AsyncButton>
                ) : null}
                {vaultKeys.length > 0 || vaultError ? (
                  <AsyncButton variant="ghost" size="xs" onClick={() => wipeLocalVault(false)}>
                    <Trash2 /> Wipe local vault
                  </AsyncButton>
                ) : null}
              </ButtonRow>
            </div>
          ) : null}
        </div>

        <Hint>
          Active session secrets live in <b>memory + sessionStorage</b> only and never touch <code>localStorage</code>.
          Enabling Windows DPAPI vault stores an OS-encrypted copy in your user AppData folder; credentials saved in the
          Integrations tab are encrypted in your cloud backend store (or local <code>backend/.env</code>).
        </Hint>
        <Console>{out}</Console>
      </Section>

      <Section
        title="GitHub — OAuth connect"
        description="One-click browser OAuth creates/installs your personal App or signs in; or import from GitHub CLI."
        action={<GithubStateBadge />}
      >
        <GithubConnect showTokenField />
      </Section>

      <Section
        title="Keys"
        action={
          <Badge variant={vaultKeys.length > 0 ? "secondary" : "outline"}>
            {vaultKeys.length > 0 ? `${vaultKeys.length} in DPAPI vault` : "session only"}
          </Badge>
        }
        footer={
          <ButtonRow className="w-full">
            <AsyncButton variant="outline" onClick={forgetSecrets}>
              <Eraser /> Forget session secrets
            </AsyncButton>
            <Button
              type="button"
              variant="destructive"
              onClick={() => (vaultKeys.length > 0 || vaultError ? setConfirmWipeAll(true) : void forgetSecretsAndVault())}
            >
              <Trash2 /> Forget session &amp; wipe local vault
            </Button>
          </ButtonRow>
        }
      >
        <Field label="OPENROUTER_API_KEY" htmlFor="sett-or" hint="Shared with Cloud setup, Review, and Quota tabs.">
          <div className="flex flex-wrap gap-2">
            <div className="min-w-56 flex-1">
              <SecretInput
                id="sett-or"
                value={openrouter}
                placeholder="sk-or-…"
                onChange={(e) => setSecret("openrouter", e.target.value)}
              />
            </div>
            <AsyncButton
              variant="outline"
              onClick={() => connectOpenRouterOAuth({ persist: false })}
              disabled={activeFlow !== null}
            >
              <Plug /> {openrouter ? "Reconnect OpenRouter" : "Connect with OpenRouter"}
            </AsyncButton>
            {activeFlow === "openrouter" ? (
              <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
                <X /> Cancel
              </Button>
            ) : null}
          </div>
        </Field>
        <Field label="COMMANDCODE_API_KEY" htmlFor="sett-cc" hint="Shared with Cloud setup and Review tabs.">
          <SecretInput
            id="sett-cc"
            value={commandcode}
            placeholder="Command Code API key"
            onChange={(e) => setSecret("commandcode", e.target.value)}
          />
        </Field>
        <Field label="OPENCODE_GO_API_KEY" htmlFor="sett-oc" hint="Shared with Cloud setup and Review tabs.">
          <SecretInput
            id="sett-oc"
            value={opencodeGo}
            placeholder="oc_sk_…"
            onChange={(e) => setSecret("opencodeGo", e.target.value)}
          />
        </Field>
      </Section>

      <Dialog open={confirmWipeAll} onOpenChange={setConfirmWipeAll}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Wipe session secrets and local PC vault?</DialogTitle>
            <DialogDescription>
              {vaultKeys.length > 0
                ? `This clears all in-memory session tokens and permanently deletes the ${vaultKeys.length} encrypted key(s) stored in your Windows DPAPI vault. You will need to re-enter your Backend API token on next launch.`
                : "This clears all in-memory session tokens and permanently deletes the local Windows DPAPI vault file on disk."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirmWipeAll(false)}>
              Cancel
            </Button>
            <AsyncButton
              variant="destructive"
              onClick={async () => {
                setConfirmWipeAll(false);
                await forgetSecretsAndVault();
              }}
            >
              <Trash2 /> Wipe everything
            </AsyncButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
