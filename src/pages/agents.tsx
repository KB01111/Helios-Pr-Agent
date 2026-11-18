import * as React from "react";
import { Download, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { AsyncButton, ButtonRow, Field, FieldGrid, Hint, Page, Section } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { apiGet, apiMut, backendUrl } from "@/lib/api";
import { refreshAgents } from "@/lib/actions";
import { useApp } from "@/lib/store";
import { errText, jsonObj, tauriInvoke } from "@/lib/tauri";
import { toastErr, toastOk } from "@/lib/toast";

const NEW = "__new__";

const TRIGGERS = [
  { value: "on_commit", label: "on_commit — every push / PR sync" },
  { value: "on_label", label: "on_label — only on matching label" },
  { value: "manual", label: "manual — API dispatch only" },
];
const MODES = [
  { value: "review_only", label: "review_only — no sandbox" },
  { value: "interactive", label: "interactive — Upstash Box" },
  { value: "ephemeral", label: "ephemeral — Vercel Sandbox" },
];

interface Form {
  name: string;
  prompt: string;
  trigger: string;
  label: string;
  mode: string;
}
const EMPTY: Form = { name: "", prompt: "", trigger: "on_commit", label: "", mode: "review_only" };

export function AgentsPage({ active }: { active: boolean }) {
  const agents = useApp((s) => s.agents);
  const picked = useApp((s) => s.pickedRepos);
  const setPicked = useApp((s) => s.setPickedRepos);
  const remote = useApp((s) => s.remoteRepos);
  const setRemote = useApp((s) => s.setRemoteRepos);
  const github = useApp((s) => s.secrets.github);

  const [selectedId, setSelectedId] = React.useState(NEW);
  const [form, setForm] = React.useState<Form>(EMPTY);
  const [repoAdd, setRepoAdd] = React.useState("");
  const [msg, setMsg] = React.useState("Agents not loaded.");
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const patch = (p: Partial<Form>) => setForm((f) => ({ ...f, ...p }));

  const allRepos = React.useMemo(() => Array.from(new Set([...picked, ...remote])).sort(), [picked, remote]);

  const fillForm = (a: any) => {
    setForm({
      name: a.name || "",
      prompt: a.system_prompt || "",
      trigger: a.trigger || "on_commit",
      label: a.trigger_label || "",
      mode: a.runtime_mode || "review_only",
    });
    const targets: string[] = Array.isArray(a.target_repos) ? a.target_repos : [];
    loadedRepos.current = targets;
    setPicked(targets);
  };

  const refresh = async (preferred?: string) => {
    setMsg("Loading agents…");
    const list = await refreshAgents();
    if (list === null) {
      setMsg(`Refresh failed (is backend up at ${backendUrl()}?)`);
      return;
    }
    setMsg(`Loaded ${list.length} agent(s).`);
    const keep = preferred !== undefined ? preferred : selectedId;
    setSelectedId(keep && list.some((a) => a.id === keep) ? keep : NEW);
  };

  // `agents` can also change from other flows (repo setup syncs an agent); count shows live.
  const selectSeq = React.useRef(0);
  const loadedRepos = React.useRef<string[]>([]);
  const onSelect = async (id: string) => {
    const seq = ++selectSeq.current; // only the newest selection may fill the form
    setSelectedId(id);
    if (id === NEW) {
      fillForm({});
      return;
    }
    try {
      const a = (await apiGet("/agents/" + encodeURIComponent(id))) || {};
      if (seq === selectSeq.current) fillForm(a);
    } catch (e) {
      if (seq === selectSeq.current) toastErr("Agent", errText(e, 200));
    }
  };

  // Repo setup can add targets to the agent that is open here; merge them in so a
  // later save does not silently drop them.
  React.useEffect(() => {
    const cur = agents.find((a) => a.id === selectedId);
    const targets = cur && Array.isArray(cur.target_repos) ? cur.target_repos : null;
    if (!targets) return;
    const added = targets.filter((r) => !loadedRepos.current.includes(r));
    loadedRepos.current = targets;
    if (added.length) setPicked(Array.from(new Set([...useApp.getState().pickedRepos, ...added])));
  }, [agents, selectedId, setPicked]);

  const save = async () => {
    const body = {
      name: form.name.trim(),
      system_prompt: form.prompt.trim(),
      target_repos: picked,
      trigger: form.trigger,
      trigger_label: form.trigger === "on_label" ? form.label.trim() || null : null,
      runtime_mode: form.mode,
    };
    if (!body.name) return toastErr("Agent", "Enter an agent name first.");
    if (body.trigger === "on_label" && !body.trigger_label) {
      return toastErr("Agent", "Enter the label name for an on_label agent.");
    }
    setMsg("Saving…");
    try {
      const saved =
        selectedId !== NEW
          ? await apiMut("PUT", "/agents/" + encodeURIComponent(selectedId), body)
          : await apiMut("POST", "/agents", body);
      const savedId = saved && saved.id;
      setMsg("Saved agent " + (savedId || body.name));
      toastOk("Agent saved", savedId || body.name);
      await refresh(savedId || undefined);
    } catch (e) {
      setMsg("Save failed: " + errText(e, 300));
      toastErr("Agent save failed", errText(e, 220));
    }
  };

  const remove = async () => {
    setConfirmDelete(false);
    if (selectedId === NEW) return toastErr("Agent", "Select an existing agent to delete.");
    setMsg("Deleting…");
    try {
      await apiMut("DELETE", "/agents/" + encodeURIComponent(selectedId));
      setMsg("Deleted.");
      toastOk("Agent deleted", selectedId.slice(0, 12));
      fillForm({});
      await refresh(NEW);
    } catch (e) {
      setMsg("Delete failed: " + errText(e, 300));
      toastErr("Delete failed", errText(e, 200));
    }
  };

  const addRepo = () => {
    const v = repoAdd.trim();
    if (!/^[^/\s]+\/[^/\s]+$/.test(v)) return toastErr("Repos", "Use owner/repo format.");
    if (!picked.includes(v)) setPicked([...picked, v]);
    setRepoAdd("");
  };

  const loadMyRepos = async () => {
    if (!github) {
      setRemote([]);
      return toastErr("GitHub", "Connect GitHub first (Settings).");
    }
    setMsg("Loading repos…");
    try {
      const raw = await tauriInvoke("github_list_repos", { token: github });
      const arr = Array.isArray(raw) ? raw : jsonObj(raw) || [];
      const names = (Array.isArray(arr) ? arr : [])
        .map((r: any) => r.full_name || r.fullName || "")
        .filter(Boolean) as string[];
      setRemote(names);
      setMsg(`Loaded ${names.length} repo(s). Tick to target.`);
      toastOk("Repos loaded", `${names.length} repositories.`);
    } catch (e) {
      setMsg("Repo load failed: " + errText(e, 300));
      toastErr("Repo load failed", errText(e, 200));
    }
  };

  const toggleRepo = (name: string, on: boolean) =>
    setPicked(on ? Array.from(new Set([...picked, name])) : picked.filter((n) => n !== name));

  return (
    <Page active={active}>
      <Section
        title="Agent configuration"
        description="Agents live in the backend. Target repos gate which webhooks fire; trigger mode decides when; execution mode decides where code runs."
        action={<Badge variant="outline">{agents.length} agent(s)</Badge>}
        footer={
          <ButtonRow className="w-full">
            <AsyncButton onClick={save}>
              <Save /> {selectedId === NEW ? "Create agent" : "Save agent"}
            </AsyncButton>
            <AsyncButton variant="outline" onClick={() => refresh()}>
              <RefreshCw /> Refresh
            </AsyncButton>
            <Button
              type="button"
              variant="destructive"
              disabled={selectedId === NEW}
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 /> Delete selected
            </Button>
            <span className="text-muted-foreground text-xs">{msg}</span>
          </ButtonRow>
        }
      >
        <FieldGrid>
          <Field label="Existing agents" htmlFor="agent-select">
            <Select value={selectedId} onValueChange={onSelect}>
              <SelectTrigger id="agent-select" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NEW}>— new agent —</SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {(a.name || a.id) + (a.id ? ` (${a.id.slice(0, 8)})` : "")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Agent name" htmlFor="agent-name">
            <Input
              id="agent-name"
              value={form.name}
              placeholder="reviewer-main"
              onChange={(e) => patch({ name: e.target.value })}
              autoComplete="off"
            />
          </Field>
        </FieldGrid>
        <FieldGrid>
          <Field label="Trigger mode" htmlFor="agent-trigger">
            <Select value={form.trigger} onValueChange={(v) => patch({ trigger: v })}>
              <SelectTrigger id="agent-trigger" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TRIGGERS.map((t) => (
                  <SelectItem key={t.value} value={t.value}>
                    {t.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Execution mode" htmlFor="agent-mode">
            <Select value={form.mode} onValueChange={(v) => patch({ mode: v })}>
              <SelectTrigger id="agent-mode" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODES.map((t) => (
                  <SelectItem key={t.value} value={t.value}>
                    {t.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </FieldGrid>
        {form.trigger === "on_label" ? (
          <Field label="Trigger label (for on_label)" htmlFor="agent-label">
            <Input
              id="agent-label"
              value={form.label}
              placeholder="review-me"
              onChange={(e) => patch({ label: e.target.value })}
              autoComplete="off"
            />
          </Field>
        ) : null}

        <Field
          label="Target repos"
          hint="Leave empty to match every repository."
        >
          <div className="rounded-md border">
            <ScrollArea className="max-h-48">
              <div className="grid gap-0.5 p-2">
                {allRepos.length ? (
                  allRepos.map((name) => (
                    <Label
                      key={name}
                      className="hover:bg-muted/60 flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 font-mono text-xs font-normal"
                    >
                      <Checkbox checked={picked.includes(name)} onCheckedChange={(v) => toggleRepo(name, v === true)} />
                      {name}
                    </Label>
                  ))
                ) : (
                  <Hint className="px-2 py-3">
                    No repos yet — connect GitHub in Settings to load your repos, or add owner/repo below.
                  </Hint>
                )}
              </div>
            </ScrollArea>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input
              value={repoAdd}
              onChange={(e) => setRepoAdd(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addRepo()}
              placeholder="owner/repo"
              className="min-w-40 flex-1"
              autoComplete="off"
              aria-label="Add repository"
            />
            <Button type="button" variant="outline" onClick={addRepo}>
              <Plus /> Add repo
            </Button>
            <AsyncButton variant="ghost" onClick={loadMyRepos}>
              <Download /> Load my repos
            </AsyncButton>
          </div>
        </Field>

        <Field label="System prompt" htmlFor="agent-prompt">
          <Textarea
            id="agent-prompt"
            rows={5}
            value={form.prompt}
            placeholder="You are a senior reviewer…"
            onChange={(e) => patch({ prompt: e.target.value })}
          />
        </Field>
      </Section>

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this agent?</DialogTitle>
            <DialogDescription>
              {form.name || selectedId} will be removed from the backend. PRs it targeted fall back to other agents or the
              default flow.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={remove}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
