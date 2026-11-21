import * as React from "react";
import {
  Activity,
  Bot,
  Brain,
  CloudCog,
  Gauge,
  Inbox,
  KeyRound,
  Moon,
  Plug,
  RefreshCw,
  Rocket,
  ScanSearch,
  Scale,
  Settings as SettingsIcon,
  Sun,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { checkBackend, fetchQuota, loadConfig, refreshAgents, syncRuns } from "@/lib/actions";
import { fetchAppHealth } from "@/lib/cloud";
import { listPrInbox } from "@/lib/cloud-agent";
import { useGh, loadRepos } from "@/lib/github";
import { refreshOAuthStatus } from "@/lib/oauth";
import { forgetSecrets, hydrateFromVault, useVaultHydrated } from "@/lib/session";
import { useApp, type TabId } from "@/lib/store";
import { tauriInvoke } from "@/lib/tauri";
import { AgentsPage } from "@/pages/agents";
import { AutoReviewPage } from "@/pages/auto-review";
import { AutonomyPage } from "@/pages/autonomy";
import { CloudSetupPage } from "@/pages/cloud-setup";
import { IntegrationsPage } from "@/pages/integrations";
import { MemoryPage } from "@/pages/memory";
import { PrInboxPage } from "@/pages/pr-inbox";
import { ProvidersPage } from "@/pages/providers";
import { QuotaPage } from "@/pages/quota";
import { ReviewPage } from "@/pages/review";
import { RunsPage } from "@/pages/runs";
import { SettingsPage } from "@/pages/settings";

interface TabDef {
  id: TabId;
  label: string;
  title: string;
  sub: React.ReactNode;
  icon: LucideIcon;
  group: "Workspace" | "Operations" | "System";
}

const TABS: TabDef[] = [
  {
    id: "setup",
    label: "Cloud setup",
    title: "Cloud PR Agent setup",
    icon: CloudCog,
    group: "Workspace",
    sub: "5-step guided setup for your always-on Cloud PR Agent, GitHub App, repo memory, and merge gate.",
  },
  {
    id: "inbox",
    label: "PR inbox",
    title: "PR inbox & merge readiness",
    icon: Inbox,
    group: "Workspace",
    sub: "Live mergeability scores, before-you-merge checklists, and inline findings across your open pull requests.",
  },
  {
    id: "memory",
    label: "Memory",
    title: "Repository memory",
    icon: Brain,
    group: "Workspace",
    sub: "Repo-specific learnings, architecture facts, and dismissed false-positive patterns.",
  },
  {
    id: "autoreview",
    label: "Auto review",
    title: "Auto review (Actions Lite)",
    icon: Rocket,
    group: "Workspace",
    sub: "Zero-host GitHub Actions mode: deploy PR Agent workflows and .pr-agent/memory.md directly to repositories.",
  },
  {
    id: "review",
    label: "Review",
    title: "Review",
    icon: ScanSearch,
    group: "Workspace",
    sub: (
      <>
        Model and provider are set in the Review tab · secrets live in memory/session or optional DPAPI vault
      </>
    ),
  },
  {
    id: "agents",
    label: "Agents & sync",
    title: "Agents & repo sync",
    icon: Bot,
    group: "Workspace",
    sub: "Configure Agent v2 profiles, merge gates, path instructions, and .pragent.yaml.",
  },
  {
    id: "runs",
    label: "Runs",
    title: "Background runs",
    icon: Activity,
    group: "Workspace",
    sub: "Runs recorded by the backend while this PC was offline.",
  },
  {
    id: "autonomy",
    label: "Autonomy",
    title: "Autonomy & escalations",
    icon: Scale,
    group: "Workspace",
    sub: "How much the decision layer may do without asking you.",
  },
  {
    id: "quota",
    label: "Quota",
    title: "Quota & autoscale",
    icon: Gauge,
    group: "Operations",
    sub: "Sandbox vCPU budgets and the scheduled Upstash Box runner.",
  },
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    icon: Workflow,
    group: "Operations",
    sub: "Install the PR-Agent CLI or drop a zero-host review workflow into a repo.",
  },
  {
    id: "integrations",
    label: "Integrations",
    title: "Integrations",
    icon: Plug,
    group: "Operations",
    sub: "Connect GitHub, Vercel, Upstash and OpenRouter — one click setup per repository.",
  },
  {
    id: "settings",
    label: "Settings",
    title: "Settings",
    icon: SettingsIcon,
    group: "System",
    sub: "Backend connection, GitHub sign-in, session keys, and Windows DPAPI vault.",
  },
];

const GROUPS = ["Workspace", "Operations", "System"] as const;

function AppSidebar() {
  const tab = useApp((s) => s.tab);
  const setTab = useApp((s) => s.setTab);
  const backend = useApp((s) => s.backend);
  const escCount = useApp((s) => s.escCount);
  const blockedPrCount = useApp((s) => s.blockedPrCount);
  const theme = useApp((s) => s.theme);
  const setTheme = useApp((s) => s.setTheme);

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2.5 px-2 py-1.5">
          <div className="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-lg">
            <ScanSearch className="size-4" />
          </div>
          <div className="grid leading-tight group-data-[collapsible=icon]:hidden">
            <span className="text-sm font-semibold">PR Agent</span>
            <span className="text-muted-foreground text-[11px]">Cloud Control · v0.3</span>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        {GROUPS.map((g) => (
          <SidebarGroup key={g}>
            <SidebarGroupLabel>{g}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {TABS.filter((t) => t.group === g).map((t) => (
                  <SidebarMenuItem key={t.id}>
                    <SidebarMenuButton
                      isActive={tab === t.id}
                      tooltip={t.label}
                      onClick={() => setTab(t.id)}
                    >
                      <t.icon />
                      <span>{t.label}</span>
                    </SidebarMenuButton>
                    {t.id === "inbox" && blockedPrCount > 0 ? (
                      <SidebarMenuBadge aria-label="blocked pull requests">{blockedPrCount}</SidebarMenuBadge>
                    ) : null}
                    {t.id === "autonomy" && escCount > 0 ? (
                      <SidebarMenuBadge aria-label="escalations waiting">{escCount}</SidebarMenuBadge>
                    ) : null}
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <div className="flex items-center gap-2 px-2 text-xs group-data-[collapsible=icon]:justify-center">
          <span
            className={
              "size-2 shrink-0 rounded-full " +
              (backend.ok === true ? "bg-success" : backend.ok === false ? "bg-destructive" : "bg-warning")
            }
          />
          <span className="text-muted-foreground truncate group-data-[collapsible=icon]:hidden">
            Backend: <b className="text-foreground font-medium">{backend.label}</b>
          </span>
        </div>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="Toggle theme"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <Sun /> : <Moon />}
              <span>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton tooltip="Clear session secrets" onClick={forgetSecrets}>
              <KeyRound />
              <span>Clear session secrets</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

function Topbar() {
  const tab = useApp((s) => s.tab);
  const setTab = useApp((s) => s.setTab);
  const backend = useApp((s) => s.backend);
  const githubConnected = useApp((s) => !!s.secrets.github);
  const openrouterConnected = useApp((s) => !!s.secrets.openrouter);
  const ghLogin = useGh((s) => s.login);
  const def = TABS.find((t) => t.id === tab) ?? TABS[0];
  const [busy, setBusy] = React.useState(false);

  const reloadAll = async () => {
    setBusy(true);
    try {
      await checkBackend(false);
      await Promise.all([
        loadConfig(),
        refreshAgents(),
        syncRuns(),
        fetchQuota(),
        refreshOAuthStatus(),
        listPrInbox({ state: "open" })
          .then((r) => useApp.getState().setBlockedPrCount(r.items.filter((p) => p.band === "blocked").length))
          .catch(() => {}),
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <header className="bg-background/80 sticky top-0 z-10 flex h-16 shrink-0 items-center gap-3 border-b px-4 backdrop-blur">
      <Tooltip>
        <TooltipTrigger asChild>
          <SidebarTrigger />
        </TooltipTrigger>
        <TooltipContent>Toggle sidebar</TooltipContent>
      </Tooltip>
      <Separator orientation="vertical" className="h-6" />
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-base leading-tight font-semibold">{def.title}</h1>
        <p className="text-muted-foreground truncate text-xs">{def.sub}</p>
      </div>
      <div className="hidden items-center gap-1.5 md:flex">
        <button
          type="button"
          onClick={() => setTab("setup")}
          className="focus-visible:ring-ring inline-flex cursor-pointer rounded-full focus-visible:ring-2 focus-visible:outline-none"
        >
          <Badge variant={githubConnected ? "success" : "outline"} className="hover:opacity-90">
            {githubConnected ? (ghLogin ? `@${ghLogin}` : "GitHub ✓") : "Connect GitHub"}
          </Badge>
        </button>
        {openrouterConnected ? (
          <button
            type="button"
            onClick={() => setTab("integrations")}
            className="focus-visible:ring-ring inline-flex cursor-pointer rounded-full focus-visible:ring-2 focus-visible:outline-none"
          >
            <Badge variant="success" className="hover:opacity-90">
              OpenRouter ✓
            </Badge>
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => setTab(backend.ok === true ? "settings" : "setup")}
          className="focus-visible:ring-ring inline-flex cursor-pointer rounded-full focus-visible:ring-2 focus-visible:outline-none"
        >
          <Badge
            variant={backend.ok === true ? "success" : backend.ok === false ? "danger" : "outline"}
            className="hover:opacity-90"
          >
            backend: {backend.label}
          </Badge>
        </button>
      </div>
      <Button variant="outline" size="sm" onClick={reloadAll} disabled={busy}>
        <RefreshCw className={busy ? "animate-spin" : ""} />
        Reload
      </Button>
    </header>
  );
}

export function App() {
  const tab = useApp((s) => s.tab);
  const theme = useApp((s) => s.theme);
  const githubToken = useApp((s) => s.secrets.github);
  const vaultHydrated = useVaultHydrated();

  // Theme: the `dark` class drives shadcn's CSS variables.
  React.useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    root.style.colorScheme = theme;
  }, [theme]);

  // Boot: hydrate Windows DPAPI vault first so BACKEND_API_TOKEN is present before backend requests fire.
  React.useEffect(() => {
    let cancelled = false;
    void (async () => {
      await hydrateFromVault();
      if (cancelled) return;
      const backendOk = await checkBackend(false);
      void loadConfig();
      void refreshAgents();
      void syncRuns();
      void fetchQuota();
      void refreshOAuthStatus();
      const ghTok = useApp.getState().secrets.github;
      if (ghTok) {
        void loadRepos();
        if (!useGh.getState().login) {
          void tauriInvoke<string>("github_login", { token: ghTok })
            .then((login) => {
              if (!cancelled && login && useApp.getState().secrets.github === ghTok) {
                useGh.setState({ login });
              }
            })
            .catch(() => {});
        }
      }

      if (backendOk) {
        const [health] = await Promise.all([
          fetchAppHealth(),
          listPrInbox({ state: "open" })
            .then((r) => {
              if (!cancelled) {
                useApp.getState().setBlockedPrCount(r.items.filter((p) => p.band === "blocked").length);
              }
            })
            .catch(() => {}),
        ]);
        if (!cancelled && health?.configured && health.ok) {
          useApp.getState().setDefaultTab("inbox");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep the GitHub login badge honest if the token disappears.
  React.useEffect(() => {
    if (!githubToken) useGh.setState({ login: "" });
  }, [githubToken]);

  return (
    <TooltipProvider delayDuration={200}>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset className="h-svh min-w-0 overflow-hidden">
          <Topbar />
          <main className="flex-1 overflow-y-auto p-4 sm:p-6">
            {vaultHydrated ? (
              <>
                <CloudSetupPage active={tab === "setup"} />
                <PrInboxPage active={tab === "inbox"} />
                <MemoryPage active={tab === "memory"} />
                <AutoReviewPage active={tab === "autoreview"} />
                <ReviewPage active={tab === "review"} />
                <AgentsPage active={tab === "agents"} />
                <RunsPage active={tab === "runs"} />
                <AutonomyPage active={tab === "autonomy"} />
                <QuotaPage active={tab === "quota"} />
                <ProvidersPage active={tab === "providers"} />
                <IntegrationsPage active={tab === "integrations"} />
                <SettingsPage active={tab === "settings"} />
              </>
            ) : (
              <div className="text-muted-foreground flex h-40 items-center justify-center text-xs">
                Loading local session…
              </div>
            )}
          </main>
        </SidebarInset>
      </SidebarProvider>
      <Toaster theme={theme} position="bottom-right" richColors closeButton />
    </TooltipProvider>
  );
}
