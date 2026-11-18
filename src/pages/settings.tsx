import * as React from "react";
import { Eraser, Wifi } from "lucide-react";
import { AsyncButton, ButtonRow, Console, Field, FieldGrid, Hint, Page, SecretInput, Section } from "@/components/common";
import { GithubConnect, GithubStateBadge } from "@/components/github-connect";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { checkBackend } from "@/lib/actions";
import { forgetSecrets } from "@/lib/session";
import { useApp } from "@/lib/store";

export function SettingsPage({ active }: { active: boolean }) {
  const backendUrl = useApp((s) => s.backendUrl);
  const setBackendUrl = useApp((s) => s.setBackendUrl);
  const backend = useApp((s) => s.backend);
  const apiToken = useApp((s) => s.secrets.api);
  const openrouter = useApp((s) => s.secrets.openrouter);
  const setSecret = useApp((s) => s.setSecret);

  const [urlDraft, setUrlDraft] = React.useState(backendUrl);
  const [out, setOut] = React.useState("Not tested.");
  React.useEffect(() => setUrlDraft(backendUrl), [backendUrl]);

  const commitUrl = () => {
    if (urlDraft.trim().replace(/\/+$/, "") === backendUrl) return;
    setBackendUrl(urlDraft);
    void checkBackend(false);
  };

  const test = async () => {
    setOut("Testing…");
    setBackendUrl(urlDraft);
    const ok = await checkBackend(true);
    const url = useApp.getState().backendUrl;
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
          <Field label="Backend API token" htmlFor="api-token" hint="Session only — cleared on window close.">
            <SecretInput
              id="api-token"
              value={apiToken}
              placeholder="optional Bearer token"
              onChange={(e) => setSecret("api", e.target.value)}
            />
          </Field>
        </FieldGrid>
        <Hint>
          Session secrets (OpenRouter, GitHub, Box, API token) live in <b>memory + sessionStorage</b> only and never touch
          localStorage. Credentials you explicitly save in the Integrations tab are written to <code>backend/.env</code>{" "}
          (git-ignored) by the Rust backend. Closing the window wipes sessionStorage; “Clear session secrets” wipes it
          immediately.
        </Hint>
        <Console>{out}</Console>
      </Section>

      <Section
        title="GitHub — OAuth connect"
        description="No backend needed: the browser opens GitHub, you enter the code, done. The token stays in memory/session."
        action={<GithubStateBadge />}
      >
        <GithubConnect showTokenField />
      </Section>

      <Section
        title="Keys"
        action={<Badge variant="outline">session only</Badge>}
        footer={
          <AsyncButton variant="destructive" onClick={forgetSecrets}>
            <Eraser /> Forget all session secrets
          </AsyncButton>
        }
      >
        <Field label="OPENROUTER_API_KEY" htmlFor="sett-or" hint="Session only — shared with the Review and Quota tabs.">
          <SecretInput
            id="sett-or"
            value={openrouter}
            placeholder="sk-or-…"
            onChange={(e) => setSecret("openrouter", e.target.value)}
          />
        </Field>
      </Section>
    </Page>
  );
}
