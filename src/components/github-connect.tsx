import * as React from "react";
import { ExternalLink, GitBranch, KeyRound, Loader2, X } from "lucide-react";
import { AsyncButton, ButtonRow, Field, Hint, SecretInput } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cancelDeviceFlow, startDeviceFlow, useGh, signInWithPat } from "@/lib/github";
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

/** GitHub sign-in (OAuth device flow or PAT). Shared by Settings and Integrations. */
export function GithubConnect({ showTokenField = false }: { showTokenField?: boolean }) {
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

  const [pat, setPat] = React.useState("");
  React.useEffect(() => setPat(""), [clearNonce]);

  return (
    <div className="grid gap-4">
      <Hint>
        Create an OAuth App at <code>github.com/settings/developers</code>, enable <b>Device Flow</b> (callback{" "}
        <code>http://localhost</code>), then paste its Client ID. The token stays in memory/sessionStorage and is only used
        for the setup calls — it is never written to <code>localStorage</code>.
      </Hint>
      <div className="grid items-end gap-4 sm:grid-cols-[1fr_auto]">
        <Field label="OAuth App Client ID" htmlFor={`${uid}-gh-client-id`} hint="Not secret — stored in localStorage.">
          <Input
            id={`${uid}-gh-client-id`}
            placeholder="Ov23…"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            autoComplete="off"
          />
        </Field>
        <ButtonRow className="sm:mb-6">
          <AsyncButton onClick={startDeviceFlow} disabled={signingIn}>
            <GitBranch /> {token ? "Sign in again" : "Sign in with GitHub"}
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
      {msg ? <Hint role="status">{msg}</Hint> : null}

      <Field label="…or paste a personal access token" htmlFor={`${uid}-gh-pat`} hint="ghp_… / github_pat_… — session only.">
        <div className="flex gap-2">
          <div className="flex-1">
            <SecretInput
              id={`${uid}-gh-pat`}
              value={pat}
              placeholder="ghp_… / github_pat_…"
              onChange={(e) => setPat(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void signInWithPat(pat).then((ok) => ok && setPat(""))}
            />
          </div>
          <AsyncButton variant="outline" onClick={async () => (await signInWithPat(pat)) && setPat("")}>
            <KeyRound /> Use PAT
          </AsyncButton>
        </div>
      </Field>

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
