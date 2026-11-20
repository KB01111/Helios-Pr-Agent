import * as React from "react";
import { ExternalLink, GitBranch, KeyRound, Loader2, Terminal, X } from "lucide-react";
import { AsyncButton, ButtonRow, Field, Hint, SecretInput } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cancelDeviceFlow, startDeviceFlow, useGh, signInWithPat } from "@/lib/github";
import { cancelOAuth, connectGitHubOAuth, importGitHubCli, useOAuth } from "@/lib/oauth";
import { useApp } from "@/lib/store";
import { openUrl } from "@/lib/tauri";

export function GithubStateBadge() {
  const connected = useApp((s) => !!s.secrets.github);
  const login = useGh((s) => s.login);
  return (
    <Badge variant={connected ? "success" : "outline"}>
      {connected ? (login ? `Connected as ${login}` : "Connected (session token)") : "Not connected"}
    </Badge>
  );
}

/** GitHub 1-click browser OAuth, CLI import, and fallback PAT/Device Flow. Shared by Settings, Auto review, and Integrations. */
export function GithubConnect({
  showTokenField = false,
  saveToBackend = false,
}: {
  showTokenField?: boolean;
  saveToBackend?: boolean;
}) {
  const uid = React.useId();
  const clientId = useGh((s) => s.clientId);
  const setClientId = useGh((s) => s.setClientId);
  const signingIn = useGh((s) => s.signingIn);
  const userCode = useGh((s) => s.userCode);
  const uri = useGh((s) => s.verificationUri);
  const msg = useGh((s) => s.signinMsg);
  const token = useApp((s) => s.secrets.github);
  const setSecret = useApp((s) => s.setSecret);
  const clearNonce = useApp((s) => s.clearNonce);

  const activeFlow = useOAuth((s) => s.activeFlow);
  const flowMessage = useOAuth((s) => s.flowMessage);
  const oauthStatus = useOAuth((s) => s.status);

  const [pat, setPat] = React.useState("");
  const [moreOpen, setMoreOpen] = React.useState(false);
  React.useEffect(() => setPat(""), [clearNonce]);
  React.useEffect(() => {
    if (signingIn || userCode) setMoreOpen(true);
  }, [signingIn, userCode]);

  const busy = activeFlow !== null || signingIn;
  const ghLogin = oauthStatus.cli.gh_login;
  const installUrl = oauthStatus.github.install_url;

  const submitPat = async () => {
    await cancelOAuth();
    if (await signInWithPat(pat)) {
      setPat("");
    }
  };

  const beginDeviceFlow = async () => {
    await cancelOAuth();
    await startDeviceFlow();
  };

  return (
    <div className="grid gap-3">
      <ButtonRow>
        <AsyncButton onClick={() => connectGitHubOAuth({ saveToBackend })} disabled={busy}>
          <GitBranch /> {token ? "Reconnect GitHub" : "Connect with GitHub"}
        </AsyncButton>
        {oauthStatus.cli.gh_available ? (
          <AsyncButton variant="outline" onClick={() => importGitHubCli(saveToBackend)} disabled={busy}>
            <Terminal /> {ghLogin ? `Use GitHub CLI (@${ghLogin})` : "Use GitHub CLI"}
          </AsyncButton>
        ) : null}
        {token && installUrl ? (
          <Button type="button" variant="outline" size="sm" onClick={() => openUrl(installUrl)}>
            <ExternalLink /> Manage repos on GitHub
          </Button>
        ) : null}
      </ButtonRow>

      {activeFlow === "github" ? (
        <div
          className="bg-muted/30 flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs"
          aria-live="polite"
        >
          <Loader2 className="text-primary size-4 shrink-0 animate-spin" />
          <span className="min-w-0 flex-1">{flowMessage}</span>
          <Button type="button" variant="ghost" size="sm" onClick={cancelOAuth}>
            <X /> Cancel
          </Button>
        </div>
      ) : null}

      <Hint className="text-xs">
        Opens GitHub in your browser for 1-click approval — on first sign-in it creates and installs your personal
        GitHub App automatically. Session tokens live in memory/sessionStorage only and never touch{" "}
        <code>localStorage</code>.
      </Hint>

      {msg && activeFlow !== "github" ? (
        <Hint className="text-xs" role="status">
          {msg}
        </Hint>
      ) : null}

      <details
        className="group"
        open={moreOpen}
        onToggle={(e) => setMoreOpen((e.currentTarget as HTMLDetailsElement).open)}
      >
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
          More sign-in options (PAT or Device flow)
        </summary>
        <div className="mt-3 grid gap-4 rounded-lg border p-3.5">
          <Field label="Personal access token" htmlFor={`${uid}-gh-pat`} hint="ghp_… / github_pat_… — session only.">
            <div className="flex gap-2">
              <div className="flex-1">
                <SecretInput
                  id={`${uid}-gh-pat`}
                  value={pat}
                  disabled={busy}
                  placeholder="ghp_… / github_pat_…"
                  onChange={(e) => setPat(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && !busy && void submitPat()}
                />
              </div>
              <AsyncButton variant="outline" disabled={busy} onClick={submitPat}>
                <KeyRound /> Use PAT
              </AsyncButton>
            </div>
          </Field>

          <div className="grid items-end gap-4 sm:grid-cols-[1fr_auto]">
            <Field
              label="OAuth App Client ID (Device Flow)"
              htmlFor={`${uid}-gh-client-id`}
              hint="Optional — only needed if using a custom OAuth App with Device Flow enabled."
            >
              <Input
                id={`${uid}-gh-client-id`}
                placeholder="Ov23…"
                value={clientId}
                disabled={busy}
                onChange={(e) => setClientId(e.target.value)}
                autoComplete="off"
              />
            </Field>
            <ButtonRow className="sm:mb-6">
              <AsyncButton variant="outline" onClick={beginDeviceFlow} disabled={busy}>
                <GitBranch /> Start device flow
              </AsyncButton>
              {signingIn ? (
                <Button type="button" variant="ghost" onClick={cancelDeviceFlow}>
                  <X /> Cancel
                </Button>
              ) : null}
            </ButtonRow>
          </div>

          {userCode ? (
            <div className="bg-muted/30 grid gap-2 rounded-lg border p-4 text-center">
              <Hint>Enter this code on GitHub:</Hint>
              <p className="font-mono text-3xl font-semibold tracking-[0.3em] select-all" aria-live="polite">
                {userCode === "…" ? <Loader2 className="mx-auto animate-spin" /> : userCode}
              </p>
              {uri ? (
                <div>
                  <Button type="button" variant="outline" size="sm" onClick={() => openUrl(uri)}>
                    <ExternalLink /> Open GitHub
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </details>

      {showTokenField ? (
        <Field label="GitHub token" htmlFor={`${uid}-gh-token`} hint="Session only — auto-filled by sign-in.">
          <SecretInput
            id={`${uid}-gh-token`}
            value={token}
            placeholder="not connected"
            onChange={(e) => setSecret("github", e.target.value)}
          />
        </Field>
      ) : null}
    </div>
  );
}
